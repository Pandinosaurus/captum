#!/usr/bin/env python3

# pyre-strict

import typing
from typing import Any, Callable, cast, Dict, List, Literal, Optional, Tuple, Union

import numpy as np
import torch
from captum._utils.gradient import _forward_layer_eval, compute_layer_gradients_and_eval
from captum._utils.typing import TargetType, TensorOrTupleOfTensorsGeneric
from captum.attr._core.gradient_shap import _scale_input
from captum.attr._core.noise_tunnel import NoiseTunnel
from captum.attr._utils.attribution import GradientAttribution, LayerAttribution
from captum.attr._utils.common import (
    _compute_conv_delta_and_format_attrs,
    _format_callable_baseline,
    _format_input_baseline,
)
from captum.log import log_usage
from torch import Tensor
from torch.nn import Module


class LayerGradientShap(LayerAttribution, GradientAttribution):
    r"""
    Implements gradient SHAP for layer based on the implementation from SHAP's
    primary author. For reference, please, view:

    https://github.com/slundberg/shap\
    #deep-learning-example-with-gradientexplainer-tensorflowkeraspytorch-models

    A Unified Approach to Interpreting Model Predictions
    https://papers.nips.cc/paper\
    7062-a-unified-approach-to-interpreting-model-predictions

    GradientShap approximates SHAP values by computing the expectations of
    gradients by randomly sampling from the distribution of baselines/references.
    It adds white noise to each input sample `n_samples` times, selects a
    random baseline from baselines' distribution and a random point along the
    path between the baseline and the input, and computes the gradient of
    outputs with respect to selected random points in chosen `layer`.
    The final SHAP values represent the expected values of
    `gradients * (layer_attr_inputs - layer_attr_baselines)`.

    GradientShap makes an assumption that the input features are independent
    and that the explanation model is linear, meaning that the explanations
    are modeled through the additive composition of feature effects.
    Under those assumptions, SHAP value can be approximated as the expectation
    of gradients that are computed for randomly generated `n_samples` input
    samples after adding gaussian noise `n_samples` times to each input for
    different baselines/references.

    In some sense it can be viewed as an approximation of integrated gradients
    by computing the expectations of gradients for different baselines.

    Current implementation uses Smoothgrad from :class:`.NoiseTunnel` in order to
    randomly draw samples from the distribution of baselines, add noise to input
    samples and compute the expectation (smoothgrad).
    """

    def __init__(
        self,
        forward_func: Callable[..., Tensor],
        layer: Module,
        device_ids: Union[None, List[int]] = None,
        multiply_by_inputs: bool = True,
    ) -> None:
        r"""
        Args:

            forward_func (Callable): The forward function of the model or any
                        modification of it
            layer (torch.nn.Module): Layer for which attributions are computed.
                        Output size of attribute matches this layer's input or
                        output dimensions, depending on whether we attribute to
                        the inputs or outputs of the layer, corresponding to
                        attribution of each neuron in the input or output of
                        this layer.
            device_ids (list[int]): Device ID list, necessary only if forward_func
                        applies a DataParallel model. This allows reconstruction of
                        intermediate outputs from batched results across devices.
                        If forward_func is given as the DataParallel model itself,
                        then it is not necessary to provide this argument.
            multiply_by_inputs (bool, optional): Indicates whether to factor
                        model inputs' multiplier in the final attribution scores.
                        In the literature this is also known as local vs global
                        attribution. If inputs' multiplier isn't factored in,
                        then this type of attribution method is also called local
                        attribution. If it is, then that type of attribution
                        method is called global.
                        More detailed can be found here:
                        https://arxiv.org/abs/1711.06104

                        In case of layer gradient shap, if `multiply_by_inputs`
                        is set to True, the sensitivity scores for scaled inputs
                        are being multiplied by
                        layer activations for inputs - layer activations for baselines.

        """
        LayerAttribution.__init__(self, forward_func, layer, device_ids)
        GradientAttribution.__init__(self, forward_func)
        self._multiply_by_inputs = multiply_by_inputs

    @typing.overload
    @log_usage(part_of_slo=True)
    def attribute(
        self,
        inputs: TensorOrTupleOfTensorsGeneric,
        baselines: Union[
            TensorOrTupleOfTensorsGeneric, Callable[..., TensorOrTupleOfTensorsGeneric]
        ],
        n_samples: int = 5,
        stdevs: Union[float, Tuple[float, ...]] = 0.0,
        target: TargetType = None,
        additional_forward_args: Optional[object] = None,
        *,
        return_convergence_delta: Literal[True],
        attribute_to_layer_input: bool = False,
    ) -> Tuple[Union[Tensor, Tuple[Tensor, ...]], Tensor]: ...

    @typing.overload
    @log_usage(part_of_slo=True)
    def attribute(
        self,
        inputs: TensorOrTupleOfTensorsGeneric,
        baselines: Union[
            TensorOrTupleOfTensorsGeneric, Callable[..., TensorOrTupleOfTensorsGeneric]
        ],
        n_samples: int = 5,
        stdevs: Union[float, Tuple[float, ...]] = 0.0,
        target: TargetType = None,
        additional_forward_args: Optional[object] = None,
        return_convergence_delta: Literal[False] = False,
        attribute_to_layer_input: bool = False,
    ) -> Union[Tensor, Tuple[Tensor, ...]]: ...

    @log_usage(part_of_slo=True)
    def attribute(
        self,
        inputs: TensorOrTupleOfTensorsGeneric,
        baselines: Union[
            TensorOrTupleOfTensorsGeneric, Callable[..., TensorOrTupleOfTensorsGeneric]
        ],
        n_samples: int = 5,
        stdevs: Union[float, Tuple[float, ...]] = 0.0,
        target: TargetType = None,
        additional_forward_args: Optional[object] = None,
        return_convergence_delta: bool = False,
        attribute_to_layer_input: bool = False,
    ) -> Union[
        Tensor, Tuple[Tensor, ...], Tuple[Union[Tensor, Tuple[Tensor, ...]], Tensor]
    ]:
        r"""
        Args:

            inputs (Tensor or tuple[Tensor, ...]): Input which are used to compute
                        SHAP attribution values for a given `layer`. If `forward_func`
                        takes a single tensor as input, a single input tensor should
                        be provided.
                        If `forward_func` takes multiple tensors as input, a tuple
                        of the input tensors should be provided. It is assumed
                        that for all given input tensors, dimension 0 corresponds
                        to the number of examples, and if multiple input tensors
                        are provided, the examples must be aligned appropriately.
            baselines (Tensor, tuple[Tensor, ...], or Callable):
                        Baselines define the starting point from which expectation
                        is computed and can be provided as:

                        - a single tensor, if inputs is a single tensor, with
                          the first dimension equal to the number of examples
                          in the baselines' distribution. The remaining dimensions
                          must match with input tensor's dimension starting from
                          the second dimension.

                        - a tuple of tensors, if inputs is a tuple of tensors,
                          with the first dimension of any tensor inside the tuple
                          equal to the number of examples in the baseline's
                          distribution. The remaining dimensions must match
                          the dimensions of the corresponding input tensor
                          starting from the second dimension.

                        - callable function, optionally takes `inputs` as an
                          argument and either returns a single tensor
                          or a tuple of those.

                        It is recommended that the number of samples in the baselines'
                        tensors is larger than one.
            n_samples (int, optional): The number of randomly generated examples
                        per sample in the input batch. Random examples are
                        generated by adding gaussian random noise to each sample.
                        Default: `5` if `n_samples` is not provided.
            stdevs    (float or tuple of float, optional): The standard deviation
                        of gaussian noise with zero mean that is added to each
                        input in the batch. If `stdevs` is a single float value
                        then that same value is used for all inputs. If it is
                        a tuple, then it must have the same length as the inputs
                        tuple. In this case, each stdev value in the stdevs tuple
                        corresponds to the input with the same index in the inputs
                        tuple.
                        Default: 0.0
            target (int, tuple, Tensor, or list, optional): Output indices for
                        which gradients are computed (for classification cases,
                        this is usually the target class).
                        If the network returns a scalar value per example,
                        no target index is necessary.
                        For general 2D outputs, targets can be either:

                        - a single integer or a tensor containing a single
                          integer, which is applied to all input examples

                        - a list of integers or a 1D tensor, with length matching
                          the number of examples in inputs (dim 0). Each integer
                          is applied as the target for the corresponding example.

                        For outputs with > 2 dimensions, targets can be either:

                        - A single tuple, which contains #output_dims - 1
                          elements. This target index is applied to all examples.

                        - A list of tuples with length equal to the number of
                          examples in inputs (dim 0), and each tuple containing
                          #output_dims - 1 elements. Each tuple is applied as the
                          target for the corresponding example.

                        Default: None
            additional_forward_args (Any, optional): If the forward function
                        requires additional arguments other than the inputs for
                        which attributions should not be computed, this argument
                        can be provided. It can contain a tuple of ND tensors or
                        any arbitrary python type of any shape.
                        In case of the ND tensor the first dimension of the
                        tensor must correspond to the batch size. It will be
                        repeated for each `n_steps` for each randomly generated
                        input sample.
                        Note that the attributions are not computed with respect
                        to these arguments.
                        Default: None
            return_convergence_delta (bool, optional): Indicates whether to return
                        convergence delta or not. If `return_convergence_delta`
                        is set to True convergence delta will be returned in
                        a tuple following attributions.
                        Default: False
            attribute_to_layer_input (bool, optional): Indicates whether to
                        compute the attribution with respect to the layer input
                        or output. If `attribute_to_layer_input` is set to True
                        then the attributions will be computed with respect to
                        layer input, otherwise it will be computed with respect
                        to layer output.
                        Note that currently it is assumed that either the input
                        or the output of internal layer, depending on whether we
                        attribute to the input or output, is a single tensor.
                        Support for multiple tensors will be added later.
                        Default: False

        Returns:
            **attributions** or 2-element tuple of **attributions**, **delta**:
            - **attributions** (*Tensor* or *tuple[Tensor, ...]*):
                        Attribution score computed based on GradientSHAP with
                        respect to layer's input or output. Attributions will always
                        be the same size as the provided layer's inputs or outputs,
                        depending on whether we attribute to the inputs or outputs
                        of the layer.
                        Attributions are returned in a tuple if
                        the layer inputs / outputs contain multiple tensors,
                        otherwise a single tensor is returned.
            - **delta** (*Tensor*, returned if return_convergence_delta=True):
                        This is computed using the property that the total
                        sum of forward_func(inputs) - forward_func(baselines)
                        must be very close to the total sum of the attributions
                        based on layer gradient SHAP.
                        Delta is calculated for each example in the input after adding
                        `n_samples` times gaussian noise to each of them. Therefore,
                        the dimensionality of the deltas tensor is equal to the
                        `number of examples in the input` * `n_samples`
                        The deltas are ordered by each input example and `n_samples`
                        noisy samples generated for it.

            Examples::

                >>> # ImageClassifier takes a single input tensor of images Nx3x32x32,
                >>> # and returns an Nx10 tensor of class probabilities.
                >>> net = ImageClassifier()
                >>> layer_grad_shap = LayerGradientShap(net, net.linear1)
                >>> input = torch.randn(3, 3, 32, 32, requires_grad=True)
                >>> # choosing baselines randomly
                >>> baselines = torch.randn(20, 3, 32, 32)
                >>> # Computes gradient SHAP of output layer when target is equal
                >>> # to 0 with respect to the layer linear1.
                >>> # Attribution size matches to the size of the linear1 layer
                >>> attribution = layer_grad_shap.attribute(input, baselines,
                                                            target=5)

        """
        # since `baselines` is a distribution, we can generate it using a function
        # rather than passing it as an input argument
        formatted_baselines = _format_callable_baseline(baselines, inputs)
        assert isinstance(formatted_baselines[0], torch.Tensor), (
            "Baselines distribution has to be provided in a form "
            "of a torch.Tensor {}.".format(formatted_baselines[0])
        )

        input_min_baseline_x_grad = LayerInputBaselineXGradient(
            self.forward_func,
            self.layer,
            device_ids=self.device_ids,
            multiply_by_inputs=self.multiplies_by_inputs,
        )

        nt = NoiseTunnel(input_min_baseline_x_grad)

        attributions = nt.attribute.__wrapped__(
            nt,  # self
            inputs,
            nt_type="smoothgrad",
            nt_samples=n_samples,
            stdevs=stdevs,
            draw_baseline_from_distrib=True,
            baselines=formatted_baselines,
            target=target,
            additional_forward_args=additional_forward_args,
            return_convergence_delta=return_convergence_delta,
            attribute_to_layer_input=attribute_to_layer_input,
        )

        return attributions

    def has_convergence_delta(self) -> bool:
        return True

    @property
    def multiplies_by_inputs(self) -> bool:
        return self._multiply_by_inputs


class LayerInputBaselineXGradient(LayerAttribution, GradientAttribution):
    def __init__(
        self,
        forward_func: Callable[..., Tensor],
        layer: Module,
        device_ids: Union[None, List[int]] = None,
        multiply_by_inputs: bool = True,
    ) -> None:
        r"""
        Args:

            forward_func (Callable): The forward function of the model or any
                        modification of it
            layer (torch.nn.Module): Layer for which attributions are computed.
                        Output size of attribute matches this layer's input or
                        output dimensions, depending on whether we attribute to
                        the inputs or outputs of the layer, corresponding to
                        attribution of each neuron in the input or output of
                        this layer.
            device_ids (list[int]): Device ID list, necessary only if forward_func
                        applies a DataParallel model. This allows reconstruction of
                        intermediate outputs from batched results across devices.
                        If forward_func is given as the DataParallel model itself,
                        then it is not necessary to provide this argument.
            multiply_by_inputs (bool, optional): Indicates whether to factor
                        model inputs' multiplier in the final attribution scores.
                        In the literature this is also known as local vs global
                        attribution. If inputs' multiplier isn't factored in,
                        then this type of attribution method is also called local
                        attribution. If it is, then that type of attribution
                        method is called global.
                        More detailed can be found here:
                        https://arxiv.org/abs/1711.06104

                        In case of layer input minus baseline x gradient,
                        if `multiply_by_inputs` is set to True, the sensitivity scores
                        for scaled inputs are being multiplied by
                        layer activations for inputs - layer activations for baselines.

        """
        LayerAttribution.__init__(self, forward_func, layer, device_ids)
        GradientAttribution.__init__(self, forward_func)
        self._multiply_by_inputs = multiply_by_inputs

    @typing.overload
    def attribute(
        self,
        inputs: Union[Tensor, Tuple[Tensor, ...]],
        baselines: Union[Tensor, Tuple[Tensor, ...]],
        target: TargetType = None,
        additional_forward_args: Optional[object] = None,
        *,
        return_convergence_delta: Literal[True],
        attribute_to_layer_input: bool = False,
        grad_kwargs: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Union[Tensor, Tuple[Tensor, ...]], Tensor]: ...

    @typing.overload
    def attribute(
        self,
        inputs: Union[Tensor, Tuple[Tensor, ...]],
        baselines: Union[Tensor, Tuple[Tensor, ...]],
        target: TargetType = None,
        additional_forward_args: Optional[object] = None,
        return_convergence_delta: Literal[False] = False,
        attribute_to_layer_input: bool = False,
        grad_kwargs: Optional[Dict[str, Any]] = None,
    ) -> Union[Tensor, Tuple[Tensor, ...]]: ...

    @log_usage(part_of_slo=True)
    def attribute(  # type: ignore
        self,
        inputs: Union[Tensor, Tuple[Tensor, ...]],
        baselines: Union[Tensor, Tuple[Tensor, ...]],
        target: TargetType = None,
        additional_forward_args: Optional[object] = None,
        return_convergence_delta: bool = False,
        attribute_to_layer_input: bool = False,
        grad_kwargs: Optional[Dict[str, Any]] = None,
    ) -> Union[
        Tensor, Tuple[Tensor, ...], Tuple[Union[Tensor, Tuple[Tensor, ...]], Tensor]
    ]:
        inputs, baselines = _format_input_baseline(inputs, baselines)
        rand_coefficient = torch.tensor(
            np.random.uniform(0.0, 1.0, inputs[0].shape[0]),
            device=inputs[0].device,
            dtype=inputs[0].dtype,
        )

        input_baseline_scaled = tuple(
            _scale_input(input, baseline, rand_coefficient)
            for input, baseline in zip(inputs, baselines)
        )
        grads, _ = compute_layer_gradients_and_eval(
            self.forward_func,
            cast(Module, self.layer),
            input_baseline_scaled,
            target,
            additional_forward_args,
            device_ids=self.device_ids,
            attribute_to_layer_input=attribute_to_layer_input,
            grad_kwargs=grad_kwargs,
        )

        attr_baselines = _forward_layer_eval(
            self.forward_func,
            baselines,
            cast(Module, self.layer),
            additional_forward_args=additional_forward_args,
            device_ids=self.device_ids,
            attribute_to_layer_input=attribute_to_layer_input,
        )

        attr_inputs = _forward_layer_eval(
            self.forward_func,
            inputs,
            cast(Module, self.layer),
            additional_forward_args=additional_forward_args,
            device_ids=self.device_ids,
            attribute_to_layer_input=attribute_to_layer_input,
        )
        attributions: Tuple[Tensor, ...]
        if self.multiplies_by_inputs:
            input_baseline_diffs = tuple(
                input - baseline for input, baseline in zip(attr_inputs, attr_baselines)
            )
            attributions = tuple(
                input_baseline_diff * grad
                for input_baseline_diff, grad in zip(input_baseline_diffs, grads)
            )
        else:
            attributions = grads

        return _compute_conv_delta_and_format_attrs(
            self,
            return_convergence_delta,
            attributions,
            baselines,
            inputs,
            additional_forward_args,
            target,
            cast(Union[Literal[True], Literal[False]], len(attributions) > 1),
        )

    def has_convergence_delta(self) -> bool:
        return True

    @property
    def multiplies_by_inputs(self) -> bool:
        return self._multiply_by_inputs
