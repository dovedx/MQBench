import torch
import torch.nn.intrinsic.qat as nniqat
from torch.nn.utils.fusion import fuse_conv_bn_eval, fuse_linear_bn_eval
from torch.quantization.fx.utils import _parent_name

import mqbench.nn.intrinsic as qnni
import mqbench.nn.intrinsic.qat as qnniqat
from mqbench.utils.registry import register_convert_function


@register_convert_function(qnni.LinearBn1d)
def convert_qnni_linearbn(model, fused_node):
    modules = dict(model.named_modules())
    fused_module = modules[fused_node.target]
    fused_linear = fuse_linear_bn(fused_module[0], fused_module[1])
    linear_parent_name, linear_name = _parent_name(fused_node.target)
    setattr(modules[linear_parent_name], linear_name, fused_linear)


@register_convert_function(qnniqat.LinearBn1d)
def convert_qnniqat_linearbn(model, fused_node):
    modules = dict(model.named_modules())
    fused_module = modules[fused_node.target]
    # Create a Linear from FusedModule.
    linear = torch.nn.Linear(fused_module.in_features, fused_module.out_features, fused_module.bias is not None)
    linear.weight = fused_module.weight
    if fused_module.bias is not None:
        linear.bias = fused_module.bias
    # Merge Linear + BN
    fused_linear = fuse_linear_bn_eval(linear.eval(), fused_module.bn)
    # We need nn.qat.linear here to export weight quantize node.
    linear.qconfig = fused_module.qconfig
    linear = torch.nn.qat.Linear.from_float(linear)
    # Attach weight fake quantize params.
    linear.weight_fake_quant = fused_module.weight_fake_quant
    linear_parent_name, linear_name = _parent_name(fused_node.target)
    setattr(modules[linear_parent_name], linear_name, fused_linear)


@register_convert_function(nniqat.ConvBn2d)
def convert_nniqat_convbn(model, fused_node):
    modules = dict(model.named_modules())
    fused_module = modules[fused_node.target]
    # Create a Conv2d from FusedModule.
    conv = torch.nn.Conv2d(fused_module.in_channels, fused_module.out_channels, fused_module.kernel_size, 
                           fused_module.stride, fused_module.padding, fused_module.dilation,
                           fused_module.groups, fused_module.bias is not None, fused_module.padding_mode)
    conv.weight = fused_module.weight
    if fused_module.bias is not None:
        conv.bias = fused_module.bias
    fused_conv = fuse_conv_bn_eval(conv.eval(), fused_module.bn)
    # We need nn.qat.conv here to export weight quantize node.
    fused_conv.qconfig = fused_module.qconfig
    fused_conv = torch.nn.qat.Conv2d.from_float(fused_conv)
    # Attach weight fake quantize params.
    fused_conv.weight_fake_quant = fused_module.weight_fake_quant
    conv_parent_name, conv_name = _parent_name(fused_node.target)
    setattr(modules[conv_parent_name], conv_name, fused_conv)


@register_convert_function(nniqat.ConvBnReLU2d)
def convert_nniqat_convbnrelu(model, fused_node):
    convert_nniqat_convbn(model, fused_node)
    modules = dict(model.named_modules())
    fused_module = modules[fused_node.target]
    # We need to Insert Relu after Merged conv.
    conv_parent_name, conv_name = _parent_name(fused_node.target)
    relu_name = 'relu'
    # Maybe has another name, but we cannot know for now.
    if not hasattr(modules[conv_parent_name], relu_name):
        setattr(modules[conv_parent_name], relu_name, 
                torch.nn.ReLU(inplace=True).train(fused_module.training))
    # Update modules.
    modules = dict(model.named_modules())
    graph = model.graph
    nodes = list(model.graph.nodes)
    with graph.inserting_after(fused_node):
        relu_node_name = relu_name if conv_parent_name == "" else "{}.{}".format(conv_parent_name, relu_name)
        assert relu_node_name in modules and isinstance(modules[relu_node_name], torch.nn.ReLU)
        inserted_node = graph.create_node("call_module", relu_node_name, (fused_node,), {})
        for _node in nodes:
            for i, _arg in enumerate(_node.args):
                if _arg == fused_node:
                    _tmp = list(_node.args)
                    _tmp[i] = inserted_node
                    _node.args = tuple(_tmp)
    model.recompile()
    model.graph.lint()