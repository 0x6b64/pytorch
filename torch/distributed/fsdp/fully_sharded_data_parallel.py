import contextlib
import copy
import functools
import math
import traceback
import warnings
from contextlib import contextmanager
from enum import auto, Enum
from typing import (
    Any,
    Callable,
    Dict,
    Generator,
    Iterable,
    Iterator,
    List,
    Optional,
    Tuple,
    Union,
)

import torch
import torch.distributed as dist
import torch.nn as nn
from torch.distributed.algorithms._checkpoint.checkpoint_wrapper import (
    _CHECKPOINT_WRAPPED_MODULE,
    ActivationWrapper,
)
from torch.distributed.algorithms._comm_hooks import LOW_PRECISION_HOOKS
from torch.distributed.fsdp._common_utils import (
    _FSDPState,
    _get_param_to_fqns,
    FSDP_PREFIX,
    FSDP_WRAPPED_MODULE,
    TrainingState,
)
from torch.distributed.fsdp._dynamo_utils import _annotate_modules_for_dynamo
from torch.distributed.fsdp._init_utils import (
    _check_orig_params_flattened,
    _get_default_comm_hook,
    _init_buffer_state,
    _init_core_state,
    _init_ignored_module_states,
    _init_param_handle_from_module,
    _init_prefetching_state,
    _init_process_group_state,
    _init_runtime_state,
    _init_state_dict_state,
    HYBRID_SHARDING_STRATEGIES,
    ProcessGroupType,
)
from torch.distributed.fsdp._runtime_utils import (
    _lazy_init,
    _post_forward,
    _post_forward_reshard,
    _pre_forward,
    _pre_forward_unshard,
    _root_pre_forward,
)
from torch.distributed.fsdp._wrap_utils import _auto_wrap
from torch.distributed.fsdp.api import (
    BackwardPrefetch,
    CPUOffload,
    FullStateDictConfig,
    LocalStateDictConfig,
    MixedPrecision,
    ShardedStateDictConfig,
    ShardingStrategy,
    StateDictConfig,
    StateDictType,
)

from ._optim_utils import (
    _broadcast_pos_dim_tensor_states,
    _broadcast_processed_optim_state_dict,
    _flatten_optim_state_dict,
    _get_param_id_to_param,
    _get_param_id_to_param_from_optim_input,
    _get_param_to_param_id,
    _get_param_to_param_id_from_optim_input,
    _optim_state_dict,
    _process_pos_dim_tensor_state,
    _rekey_sharded_optim_state_dict,
)
from ._state_dict_utils import (
    _register_load_state_dict_post_hooks,
    _register_load_state_dict_pre_hooks,
    _register_state_dict_hooks,
    _register_state_dict_pre_hooks,
)
from ._unshard_param_utils import (
    _deregister_orig_params,
    _register_flat_param,
    _register_orig_params,
    _unshard_params,
)
from ._utils import p_assert
from .flat_param import FlatParameter, FlatParamHandle
from .wrap import _FSDPPolicy


__all__ = [
    "FullyShardedDataParallel",
    "OptimStateKeyType",
]


FLAT_PARAM = "_flat_param"


class OptimStateKeyType(Enum):
    PARAM_NAME = auto()
    PARAM_ID = auto()


class FullyShardedDataParallel(nn.Module, _FSDPState):
    """
    A wrapper for sharding Module parameters across data parallel workers. This
    is inspired by `Xu et al.`_ as well as the ZeRO Stage 3 from DeepSpeed_.
    FullyShardedDataParallel is commonly shortened to FSDP.

    .. _`Xu et al.`: https://arxiv.org/abs/2004.13336
    .. _DeepSpeed: https://www.deepspeed.ai/

    Example::

        >>> # xdoctest: +SKIP("undefined variables")
        >>> import torch
        >>> from torch.distributed.fsdp import FullyShardedDataParallel as FSDP
        >>> torch.cuda.set_device(device_id)
        >>> sharded_module = FSDP(my_module)
        >>> optim = torch.optim.Adam(sharded_module.parameters(), lr=0.0001)
        >>> x = sharded_module(x, y=3, z=torch.Tensor([1]))
        >>> loss = x.sum()
        >>> loss.backward()
        >>> optim.step()

    .. warning::
        The optimizer must be initialized *after* the module has been wrapped,
        since FSDP will shard parameters in-place and this will break any
        previously initialized optimizers.

    .. warning::
        If the destination CUDA device has ID ``dev_id``, either (1)
        ``module`` should already be placed on that device, (2) the device
        should be set using ``torch.cuda.set_device(dev_id)``, or (3)
        ``dev_id`` should be passed into the ``device_id`` constructor
        argument. This FSDP instance's compute device will be that destination
        device. For (1) and (3), the FSDP initialization always occurs on GPU.
        For (2), the FSDP initialization happens on ``module`` 's current
        device, which may be CPU.

    .. warning::
        FSDP currently does not support gradient accumulation outside
        ``no_sync()`` when using CPU offloading. Trying to do so yields
        incorrect results since FSDP will use the newly-reduced gradient
        instead of accumulating with any existing gradient.

    .. warning::
        Changing the original parameter variable names after construction will
        lead to undefined behavior.

    .. warning::
        Passing in `sync_module_states=True` flag requires module to be put
        on GPU, or to use ``device_id`` argument to specify a CUDA device that
        FSDP will move module to. This is because ``sync_module_states=True``
        requires GPU communication.

    .. warning::
        As of PyTorch 1.12, FSDP only offers limited support for shared parameters
        (for example, setting one ``Linear`` layer's weight to another's). In
        particular, modules that share parameters must be wrapped as part of the
        same FSDP unit. If enhanced shared parameter support is needed for your
        use case, please ping https://github.com/pytorch/pytorch/issues/77724

    .. note:
        Attempting to run the forward pass of a submodule that is contained in an
        FSDP instance is not supported and will result in errors. This is because the
        submodule's parameters will be sharded, but it itself is not an FSDP instance,
        so its forward pass will not all-gather the full parameters appropriately.
        This could potentially happen when attempting to run only the encoder of a
        encoder-decoder model, and the encoder is not wrapped in its own FSDP instance. To
        resolve this, please wrap the submodule in its own FSDP unit.

    .. note::
        Inputs into FSDP ``forward`` function will be moved to compute device
        (same device FSDP module is on) before running ``forward``, so user does
        not have to manually move inputs from CPU -> GPU.

    Args:
        module (nn.Module):
            This is the module to be wrapped with FSDP.
        process_group: Optional[Union[ProcessGroup, Tuple[ProcessGroup, ProcessGroup]]]
            This is the process group used for collective communications and
            the one over which the model is sharded. For hybrid sharding strategies such as
            ``ShardingStrategy.HYBRID_SHARD`` users can
            pass in a tuple of process groups representing the groups to shard and replicate across,
            respectively.
        sharding_strategy (Optional[ShardingStrategy]):
            This configures the sharding strategy used by FSDP, which may trade
            off memory saving and communication overhead. See
            :class:`ShardingStrategy` for details. (Default: ``FULL_SHARD``)
        cpu_offload (Optional[CPUOffload]):
            This configures CPU offloading. If this is set to ``None``, then
            no CPU offloading happens. See :class:`CPUOffload` for details.
            (Default: ``None``)
        auto_wrap_policy (Optional[Union[Callable[[nn.Module, bool, int], bool], _FSDPPolicy]]):
            This is either ``None``, an ``_FSDPPolicy``, or a callable of
            a fixed signature. If it is ``None``, then ``module`` is wrapped
            with only a top-level FSDP instance without any nested wrapping. If
            it is an ``_FSDPPolicy``, then the wrapping follows the given
            policy. ``ModuleWrapPolicy`` in ``torch.distributed.fsdp.wrap.py``
            is an example. If it is a callable, then it should take in three
            arguments ``module: nn.Module``, ``recurse: bool``, and
            ``nonwrapped_numel: int`` and should return a ``bool`` specifying
            whether the passed-in ``module`` should be wrapped if
            ``recurse=False`` or if the traversal should continue down the
            subtree if ``recurse=True``. Additional custom arguments may be
            added to the callable. The ``size_based_auto_wrap_policy`` in
            ``torch.distributed.fsdp.wrap.py`` gives an example callable that
            wraps a module if the parameters in its subtree exceed 100M numel.
            A good practice is to print the model after wrapping and adjust as
            needed.

            Example::

                >>> def custom_auto_wrap_policy(
                >>>     module: nn.Module,
                >>>     recurse: bool,
                >>>     nonwrapped_numel: int,
                >>>     # Additional custom arguments
                >>>     min_num_params: int = int(1e8),
                >>> ) -> bool:
                >>>     return nonwrapped_numel >= min_num_params
                >>> # Configure a custom `min_num_params`
                >>> my_auto_wrap_policy = functools.partial(custom_auto_wrap_policy, min_num_params=int(1e5))

        backward_prefetch (Optional[BackwardPrefetch]):
            This configures explicit backward prefetching of all-gathers. See
            :class:`BackwardPrefetch` for details. (Default: ``BACKWARD_PRE``)
        mixed_precision (Optional[MixedPrecision]):
            This configures native mixed precision for FSDP. If this is set to
            ``None``, then no mixed precision is used. Otherwise, parameter,
            buffer, and gradient reduction dtypes can be set. See
            :class:`MixedPrecision` for details. (Default: ``None``)
        ignored_modules (Optional[Iterable[torch.nn.Module]]): Modules whose
            own parameters and child modules' parameters and buffers are
            ignored by this instance. None of the modules directly in
            ``ignored_modules`` should be :class:`FullyShardedDataParallel`
            instances, and any child modules that are already-constructed
            :class:`FullyShardedDataParallel` instances will not be ignored if
            they are nested under this instance. This argument may be used to
            avoid sharding specific parameters at module granularity when using an
            ``auto_wrap_policy`` or if parameters' sharding is not managed by
            FSDP. (Default: ``None``)
        param_init_fn (Optional[Callable[[nn.Module], None]]):
            A ``Callable[torch.nn.Module] -> None`` that
            specifies how modules that are currently on the meta device should be initialized
            onto an actual device. Note that as of v1.12, we detect modules on the meta
            device via ``is_meta`` check and apply a default initialization that calls
            ``reset_parameters`` method on the passed in ``nn.Module`` if ``param_init_fn``
            is not specified, otherwise we run ``param_init_fn`` to initialize the passed
            in ``nn.Module``. In particular, this means that if ``is_meta=True`` for any
            module parameters for modules that will be wrapped with FSDP and ``param_init_fn``
            is not specified, we assume your module properly implements a ``reset_parameters()``
            and will throw errors if not. Note that additionally, we offer support for modules
            initialized with torchdistX's (https://github.com/pytorch/torchdistX)
            ``deferred_init`` API. In this case, deferred modules would be initialized
            by a default initialization function that calls torchdistX's
            ``materialize_module``, or the passed in ``param_init_fn``, if it is not
            ``None``. The same ``Callable`` is applied to initialize all meta modules.
            Note that this initialization function is applied before doing any FSDP sharding
            logic.

            Example::

                >>> # xdoctest: +SKIP("undefined variables")
                >>> module = MyModule(device="meta")
                >>> def my_init_fn(module):
                >>>     # responsible for initializing a module, such as with reset_parameters
                >>>     ...
                >>> fsdp_model = FSDP(module, param_init_fn=my_init_fn, auto_wrap_policy=size_based_auto_wrap_policy)
                >>> print(next(fsdp_model.parameters()).device) # current CUDA device
                >>> # With torchdistX
                >>> module = deferred_init.deferred_init(MyModule, device="cuda")
                >>> # Will initialize via deferred_init.materialize_module().
                >>> fsdp_model = FSDP(module, auto_wrap_policy=size_based_auto_wrap_policy)

        device_id (Optional[Union[int, torch.device]]): An ``int`` or ``torch.device``
            describing the CUDA device the FSDP module should be moved to determining where
            initialization such as sharding takes place. If this argument is not specified
            and ``module`` is on CPU, we issue a warning mentioning that this argument can
            be specified for faster initialization. If specified, resulting FSDP instances
            will reside on this device, including moving ignored modules' parameters if
            needed. Note that if ``device_id`` is specified but ``module`` is already on a
            different CUDA device, an error will be thrown. (Default: ``None``)
        sync_module_states (bool): If ``True``, each individually wrapped FSDP unit will broadcast
            module parameters from rank 0 to ensure they are the same across all ranks after
            initialization. This helps ensure model parameters are the same across ranks
            before starting training, but adds communication overhead to ``__init__``, as at least
            one broadcast is triggered per individually wrapped FSDP unit.
            This can also help load checkpoints taken by ``state_dict`` and to be loaded by
            ``load_state_dict`` in a memory efficient way. See documentation for
            :class:`FullStateDictConfig` for an example of this. (Default: ``False``)
        forward_prefetch (bool): If ``True``, then FSDP *explicitly* prefetches
            the next upcoming all-gather while executing in the forward pass.
            This may improve communication and computation overlap for CPU
            bound workloads. This should only be used for static graph models
            since the forward order is fixed based on the first iteration's
            execution. (Default: ``False``)
        limit_all_gathers (bool): If ``False``, then FSDP allows the CPU
            thread to schedule all-gathers without any extra synchronization.
            If ``True``, then FSDP explicitly synchronizes the CPU thread to
            prevent too many in-flight all-gathers. This ``bool`` only affects
            the sharded strategies that schedule all-gathers. Enabling this can
            help lower the number of CUDA malloc retries.
    """

    def __init__(
        self,
        module: nn.Module,
        process_group: ProcessGroupType = None,
        sharding_strategy: Optional[ShardingStrategy] = None,
        cpu_offload: Optional[CPUOffload] = None,
        auto_wrap_policy: Optional[Union[Callable, _FSDPPolicy]] = None,
        backward_prefetch: Optional[BackwardPrefetch] = BackwardPrefetch.BACKWARD_PRE,
        mixed_precision: Optional[MixedPrecision] = None,
        ignored_modules: Optional[Iterable[torch.nn.Module]] = None,
        param_init_fn: Optional[Callable[[nn.Module], None]] = None,
        device_id: Optional[Union[int, torch.device]] = None,
        sync_module_states: bool = False,
        forward_prefetch: bool = False,
        limit_all_gathers: bool = False,
        use_orig_params: bool = False,
    ):
        torch._C._log_api_usage_once("torch.distributed.fsdp")
        super().__init__()
        _init_ignored_module_states(self, module, ignored_modules)

        # Add module annotations for Dynamo support (see function for details)
        _annotate_modules_for_dynamo(module, self._ignored_modules, use_orig_params)

        # Initializes self.process_group, along with rank and world size. This will
        # also set another attribute, _inter_node_pg, to control the process group
        # over which sharding occurs, if sharding_strategy is {HYBRID_SHARD, _HYBRID_SHARD_ZERO2}.
        # Note that this is done before auto_wrapping, so that child FSDP modules simply pick up
        # the same process group state as the root FSDP module.
        _init_process_group_state(
            self, process_group, sharding_strategy, auto_wrap_policy
        )
        if auto_wrap_policy is not None:
            auto_wrap_kwargs = {
                "module": module,
                "auto_wrap_policy": auto_wrap_policy,
                "wrapper_cls": FullyShardedDataParallel,
                "ignored_modules": self._ignored_modules,
                "ignored_params": self._ignored_params,
                "only_wrap_children": True,  # avoid double wrapping the root
            }
            fsdp_kwargs = {
                "process_group": process_group,
                "sharding_strategy": sharding_strategy,
                "cpu_offload": cpu_offload,
                "backward_prefetch": backward_prefetch,
                "mixed_precision": mixed_precision,
                "param_init_fn": param_init_fn,
                "device_id": device_id,
                "sync_module_states": sync_module_states,
                "forward_prefetch": forward_prefetch,
                "limit_all_gathers": limit_all_gathers,
                "use_orig_params": use_orig_params,
            }
            if sharding_strategy in HYBRID_SHARDING_STRATEGIES:
                # Share root process groups with children to maintain
                # the invariant that all FSDP modules will have the same
                # process groups.
                fsdp_kwargs["process_group"] = (self.process_group, self._inter_node_pg)

            _auto_wrap(auto_wrap_kwargs, fsdp_kwargs, FullyShardedDataParallel)

        backward_prefetch_limit = 1
        forward_prefetch_limit = 1
        _init_core_state(
            self,
            sharding_strategy,
            mixed_precision,
            cpu_offload,
            limit_all_gathers,
            use_orig_params,
            backward_prefetch_limit,
            forward_prefetch_limit,
        )
        _init_runtime_state(self)
        _init_prefetching_state(self, backward_prefetch, forward_prefetch)
        _init_buffer_state(self, module)
        _init_param_handle_from_module(
            self,
            module,
            device_id,
            param_init_fn,
            sync_module_states,
            FullyShardedDataParallel,
        )
        self._fsdp_wrapped_module = module
        if not use_orig_params:
            _check_orig_params_flattened(self, self._ignored_params)
            _register_flat_param(self, self)

        # Delete to avoid keeping references after the constructor
        delattr(self, "_ignored_params")

        # `_state_dict_type` controls the `state_dict()` behavior, which is
        # implemented using post-save and pre-load hooks
        _init_state_dict_state(self)
        _register_state_dict_pre_hooks(self)
        _register_state_dict_hooks(self)
        _register_load_state_dict_pre_hooks(self)
        _register_load_state_dict_post_hooks(self)

    @property
    def module(self) -> nn.Module:
        """
        Returns the wrapped module (like :class:`DistributedDataParallel`).
        """
        # FSDP's `.module` must refer to the innermost wrapped module when
        # composing with other module wrappers in order for state dict to work
        if isinstance(self._fsdp_wrapped_module, ActivationWrapper):
            return getattr(self._fsdp_wrapped_module, _CHECKPOINT_WRAPPED_MODULE)
        return self._fsdp_wrapped_module

    @property
    def _has_params(self) -> bool:
        """Returns whether this FSDP instance manages any parameters."""
        return hasattr(self, "_handles") and len(self._handles) > 0

    @property
    def _flat_param(self) -> Optional[FlatParameter]:
        return self._handles[0].flat_param if self._handles else None

    def __getattr__(self, name: str) -> Any:
        """Forward missing attributes to the wrapped module."""
        try:
            return super().__getattr__(name)  # defer to nn.Module's logic
        except AttributeError:
            return getattr(self._fsdp_wrapped_module, name)

    def __getitem__(self, key: int) -> Any:
        """Forward indexing calls in case the module is an ``nn.Sequential``."""
        if hasattr(self, FSDP_WRAPPED_MODULE):
            return self._fsdp_wrapped_module.__getitem__(key)  # type: ignore[operator]
        return super().__getitem__(key)

    def check_is_root(self) -> bool:
        _lazy_init(self, self)
        assert self._is_root is not None
        return self._is_root

    @staticmethod
    def fsdp_modules(
        module: nn.Module,
        root_only: bool = False,
    ) -> List["FullyShardedDataParallel"]:
        """
        Returns all nested FSDP instances, possibly including ``module`` itself
        and only including FSDP root modules if ``root_only=True``.

        Args:
            module (torch.nn.Module): Root module, which may or may not be an
                ``FSDP`` module.
            root_only (bool): Whether to return only FSDP root modules.
                (Default: ``False``)

        Returns:
            List[FullyShardedDataParallel]: FSDP modules that are nested in
            the input ``module``.
        """
        return [
            submodule
            for submodule in module.modules()
            if isinstance(submodule, FullyShardedDataParallel)
            and (not root_only or submodule.check_is_root())
        ]

    @staticmethod
    def _fsdp_handles(module: nn.Module) -> List[FlatParamHandle]:
        """
        Returns all nested FSDP instances' handles in the module hierarchy
        rooted at ``module``.
        """
        return [
            handle
            for fsdp_module in FullyShardedDataParallel.fsdp_modules(module)
            for handle in fsdp_module._handles
        ]

    def apply(self, fn: Callable[[nn.Module], None]) -> "FullyShardedDataParallel":
        r"""Applies ``fn`` recursively to every submodule (as returned by ``.children()``)
        as well as self. Typical use includes initializing the parameters of a model
        (see also :ref:`nn-init-doc`).

        Compared to ``torch.nn.Module.apply``, this version additionally gathers
        the full parameters before applying ``fn``. It should not be called from
        within another ``summon_full_params`` context.

        Args:
            fn (:class:`Module` -> None): function to be applied to each submodule

        Returns:
            Module: self
        """
        uninitialized = self._is_root is None
        self._assert_state(TrainingState.IDLE)
        with self._summon_full_params(recurse=False, writeback=True):
            ret = super().apply(fn)

        # Reset lazy init that might be called by _summon_full_params, since
        # it could have set is_root incorrectly for non-root FSDP instances.
        if uninitialized and self._is_root:
            for module in self.fsdp_modules(self):
                module._reset_lazy_init()

        return ret

    def _mixed_precision_enabled_for_buffers(self) -> bool:
        """
        Returns if the user explicitly enabled buffer mixed precision.

        NOTE: Unlike parameters and gradient reduction, buffer mixed precision
        is applied at the FSDP instance level, not the ``FlatParameter`` level,
        which may be different for the composable code path.
        """
        return self.mixed_precision.buffer_dtype is not None

    def _low_precision_hook_enabled(self) -> bool:
        """
        Wether a low precision hook is registered or not.
        """
        return (
            self._communication_hook is not None
            and self._communication_hook in LOW_PRECISION_HOOKS
        )

    def _reset_lazy_init(self) -> None:
        """
        Reset instance so :func:`_lazy_init` will run on the next forward.
        """
        self._is_root: Optional[bool] = None

    @staticmethod
    def set_state_dict_type(
        module: nn.Module,
        state_dict_type: StateDictType,
        state_dict_config: Optional[StateDictConfig] = None,
    ) -> Tuple[StateDictType, StateDictConfig]:
        """
        Set the ``state_dict_type`` and the corresponding (optional)
        configurations of all the descendant FSDP modules of the target module.
        The target module does not have to be a FSDP module. If the target
        module is a FSDP module, its ``state_dict_type`` will also be changed.

        .. note:: This API should be called for only the top-level (root)
            module.

        .. note:: This API enables users to transparently use the conventional
            ``state_dict`` API to take model checkpoints in cases where the
            root FSDP module is wrapped by another ``nn.Module``. For example,
            the following will ensure ``state_dict`` is called on all non-FSDP
            instances, while dispatching into `sharded_state_dict` implementation
            for FSDP:

        Example::

            >>> # xdoctest: +SKIP("undefined variables")
            >>> model = DDP(FSDP(...))
            >>> FSDP.set_state_dict_type(
            >>>     model,
            >>>     StateDictType.SHARDED_STATE_DICT,
            >>>     ShardedStateDictConfig(offload_to_cpu=True),
            >>> )
            >>> checkpoint = model.state_dict()

        Args:
            module (torch.nn.Module): Root module.
            state_dict_type (StateDictType): the desired ``state_dict_type`` to set.
            state_dict_config (Optional[StateDictConfig]): the configuration for the
                target ``state_dict_type``.
        """
        _state_dict_type_to_config = {
            StateDictType.FULL_STATE_DICT: FullStateDictConfig,
            StateDictType.LOCAL_STATE_DICT: LocalStateDictConfig,
            StateDictType.SHARDED_STATE_DICT: ShardedStateDictConfig,
        }

        prev_state_dict_type = None
        prev_state_dict_config = None
        # Use the default config if a state_dict config is not set.
        if state_dict_config is None:
            state_dict_config = _state_dict_type_to_config[state_dict_type]()
        for submodule in FullyShardedDataParallel.fsdp_modules(module):
            if prev_state_dict_type is None:
                prev_state_dict_type = submodule._state_dict_type
            if prev_state_dict_config is None:
                prev_state_dict_config = submodule._state_dict_config
            if prev_state_dict_type != submodule._state_dict_type:
                raise RuntimeError("All FSDP module should the same state_dict_type.")
            if not isinstance(
                submodule._state_dict_config, type(prev_state_dict_config)
            ):
                raise RuntimeError(
                    "All FSDP modules should have the same type of state_dict_config."
                )

            expected_state_dict_config_type = _state_dict_type_to_config[
                state_dict_type
            ]
            if expected_state_dict_config_type != type(state_dict_config):
                raise RuntimeError(
                    f"Expected state_dict_config of type {expected_state_dict_config_type} "
                    f"but got {type(state_dict_config)}"
                )
            submodule._state_dict_type = state_dict_type
            submodule._state_dict_config = state_dict_config

        return prev_state_dict_type, prev_state_dict_config

    @staticmethod
    @contextlib.contextmanager
    def state_dict_type(
        module: nn.Module,
        state_dict_type: StateDictType,
        state_dict_config: Optional[StateDictConfig] = None,
    ) -> Generator:
        """
        A context manager to set the ``state_dict_type`` of all the descendant
        FSDP modules of the target module. This context manager has the same
        functions as :meth:`set_state_dict_type`. Read the document of
        :meth:`set_state_dict_type` for the detail.

        Example::

            >>> # xdoctest: +SKIP("undefined variables")
            >>> model = DDP(FSDP(...))
            >>> with FSDP.state_dict_type(
            >>>     model,
            >>>     StateDictType.SHARDED_STATE_DICT,
            >>> ):
            >>>     checkpoint = model.state_dict()

        Args:
            module (torch.nn.Module): Root module.
            state_dict_type (StateDictType): the desired ``state_dict_type`` to set.
            state_dict_config (Optional[StateDictConfig]): the configuration for the
                target ``state_dict_type``.
        """
        prev_state_dict_type = None
        prev_state_dict_config = None
        try:
            (
                prev_state_dict_type,
                prev_state_dict_config,
            ) = FullyShardedDataParallel.set_state_dict_type(
                module, state_dict_type, state_dict_config
            )
            yield
        except Exception as e:
            raise e
        else:
            assert prev_state_dict_type is not None
            assert prev_state_dict_config is not None
        finally:
            if prev_state_dict_type is not None and prev_state_dict_config is not None:
                FullyShardedDataParallel.set_state_dict_type(
                    module, prev_state_dict_type, prev_state_dict_config
                )

    def forward(self, *args: Any, **kwargs: Any) -> Any:
        """
        Runs the forward pass for the wrapped module, inserting FSDP-specific
        pre- and post-forward sharding logic.
        """
        with torch.autograd.profiler.record_function(
            "FullyShardedDataParallel.forward"
        ):
            unused = None
            _root_pre_forward(self, self, unused)
            unshard_fn = functools.partial(_pre_forward_unshard, self, self._handles)
            reshard_fn = functools.partial(_post_forward_reshard, self, self._handles)
            args, kwargs = _pre_forward(
                self, self._handles, unshard_fn, self._fsdp_wrapped_module, args, kwargs
            )
            for handle in self._handles:
                p_assert(
                    handle.flat_param.device == self.compute_device,
                    "Expected `FlatParameter` to be on the compute device "
                    f"{self.compute_device} but got {handle.flat_param.device}",
                )
            output = self._fsdp_wrapped_module(*args, **kwargs)
            return _post_forward(
                self, self._handles, reshard_fn, unused, unused, output
            )

    @staticmethod
    @contextlib.contextmanager
    def summon_full_params(
        module,
        recurse: bool = True,
        writeback: bool = True,
        rank0_only: bool = False,
        offload_to_cpu: bool = False,
        with_grads: bool = False,
    ) -> Generator:
        r"""A context manager to expose full params for FSDP instances.
        Can be useful *after* forward/backward for a model to get
        the params for additional processing or checking. It can take a non-FSDP
        module and will summon full params for all contained FSDP modules as
        well as their children, depending on the ``recurse`` argument.

        .. note:: This can be used on inner FSDPs.
        .. note:: This can *not* be used within a forward or backward pass. Nor
            can forward and backward be started from within this context.
        .. note:: Parameters will revert to their local shards after the context
            manager exits, storage behavior is the same as forward.
        .. note:: The full parameters can be modified, but only the portion
            corresponding to the local param shard will persist after the
            context manager exits (unless ``writeback=False``, in which case
            changes will be discarded). In the case where FSDP does not shard
            the parameters, currently only when ``world_size == 1``, or ``NO_SHARD``
            config, the modification is persisted regardless of ``writeback``.
        .. note:: This method works on modules which are not FSDP themselves but
            may contain multiple independent FSDP units. In that case, the given
            arguments will apply to all contained FSDP units.

        .. warning:: Note that ``rank0_only=True`` in conjunction with
            ``writeback=True`` is not currently supported and will raise an
            error. This is because model parameter shapes would be different
            across ranks within the context, and writing to them can lead to
            inconsistency across ranks when the context is exited.

        .. warning:: Note that ``offload_to_cpu`` and ``rank0_only=False`` will
            result in full parameters being redundantly copied to CPU memory for
            GPUs that reside on the same machine, which may incur the risk of
            CPU OOM. It is recommended to use ``offload_to_cpu`` with
            ``rank0_only=True``.

        Args:
            recurse (bool, Optional): recursively summon all params for nested
                FSDP instances (default: True).
            writeback (bool, Optional): if ``False``, modifications to params are
                discarded after the context manager exits;
                disabling this can be slightly more efficient (default: True)
            rank0_only (bool, Optional): if ``True``, full parameters are
                materialized on only global rank 0. This means that within the
                context, only rank 0 will have full parameters and the other
                ranks will have sharded parameters. Note that setting
                ``rank0_only=True`` with ``writeback=True`` is not supported,
                as model parameter shapes will be different across ranks
                within the context, and writing to them can lead to
                inconsistency across ranks when the context is exited.
            offload_to_cpu (bool, Optional): If ``True``, full parameters are
                offloaded to CPU. Note that this offloading currently only
                occurs if the parameter is sharded (which is only not the case
                for world_size = 1 or ``NO_SHARD`` config). It is recommended
                to use ``offload_to_cpu`` with ``rank0_only=True`` to avoid
                redundant copies of model parameters being offloaded to the same CPU memory.
            with_grads (bool, Optional): If ``True``, gradients are also
                unsharded with the parameters. Currently, this is only
                supported when passing ``use_orig_params=True`` to the FSDP
                constructor and ``offload_to_cpu=False`` to this method.
                (Default: ``False``)
        """
        # Note that we specify root_only as FSDP roots will handle summoning
        # child FSDP instances based on recurse argument.
        root_fsdp_modules = FullyShardedDataParallel.fsdp_modules(
            module, root_only=True
        )
        # Summon all params for all FSDP instances
        with contextlib.ExitStack() as stack:
            for module in root_fsdp_modules:
                stack.enter_context(
                    module._summon_full_params(
                        recurse=recurse,
                        writeback=writeback,
                        rank0_only=rank0_only,
                        offload_to_cpu=offload_to_cpu,
                        with_grads=with_grads,
                    )
                )
            # Yield to the caller, with full params in all FSDP instances.
            yield
        # Exiting from the ExitStack will reshard all params.
        return

    @contextlib.contextmanager
    def _summon_full_params(
        self,
        recurse: bool = True,
        writeback: bool = True,
        rank0_only: bool = False,
        offload_to_cpu: bool = False,
        with_grads: bool = False,
    ):
        if with_grads and (offload_to_cpu or not self._use_orig_params):
            raise NotImplementedError(
                f"with_grads={with_grads} "
                f"use_orig_params={self._use_orig_params} "
                f"offload_to_cpu={offload_to_cpu} "
                f"is not supported yet"
            )
        if writeback and rank0_only:
            raise ValueError(
                "writeback=True and rank0_only=True is not supported, as model "
                "parameter shapes will be different across ranks, and writing "
                "to them can lead to inconsistencies across ranks when the "
                "context is exited."
            )
        if offload_to_cpu and not rank0_only:
            warnings.warn(
                "offload_to_cpu and rank0_only=False will result in "
                "full parameters being redundantly copied to CPU memory for "
                "GPUs that reside on the same machine, which may incur the risk of "
                "CPU OOM. It is recommended to use ``offload_to_cpu`` with "
                "rank0_only=True."
            )

        if recurse:
            with contextlib.ExitStack() as stack:
                for module in self.fsdp_modules(self):
                    stack.enter_context(
                        module._summon_full_params(
                            recurse=False,
                            writeback=writeback,
                            rank0_only=rank0_only,
                            offload_to_cpu=offload_to_cpu,
                            with_grads=with_grads,
                        )
                    )
                yield
            return

        _lazy_init(self, self)
        with _unshard_params(
            module=self,
            state=self,
            writeback=writeback,
            rank0_only=rank0_only,
            offload_to_cpu=offload_to_cpu,
            with_grads=with_grads,
        ):
            try:
                self.training_state = TrainingState.SUMMON_FULL_PARAMS
                yield
            finally:
                self.training_state = TrainingState.IDLE

    @contextlib.contextmanager
    def _deregister_orig_params_ctx(self):
        """
        This deregisters the original parameters and exposes the
        :class:`FlatParameter` s. If a :class:`FlatParameter` is sharded, then
        this refreshes the sharded views before exiting. This method shouuld
        only be called when using the original parameters.
        """
        p_assert(
            self._use_orig_params,
            "`_deregister_orig_params_ctx()` should only be called when "
            "`_use_orig_params=True`",
        )
        for fsdp_module in self.fsdp_modules(self):
            _deregister_orig_params(fsdp_module, fsdp_module)
        try:
            yield
        finally:
            for fsdp_module in self.fsdp_modules(self):
                _register_orig_params(fsdp_module, fsdp_module)

    def _apply(self, *args, **kwargs):
        """
        When using the original parameters, this deregisters the original
        parameters and exposes the :class:`FlatParameter` s before calling
        ``_apply()``.
        """
        # When using the original parameters: Since (1) the `FlatParameter`s
        # own the storage and (2) `_apply()` is the subroutine underlying the
        # most common storage-changing ops like `to()` and `cuda()`, we
        # override `_apply()` to have the storage change directly performed on
        # the `FlatParameter`s instead of applying to the original parameters
        # and then writing back to the `FlatParameter`s.
        with (
            self._deregister_orig_params_ctx()
            if self._use_orig_params
            else contextlib.suppress()
        ):
            return super()._apply(*args, **kwargs)

    def named_buffers(
        self,
        *args,
        **kwargs,
    ) -> Iterator[Tuple[str, torch.Tensor]]:
        """
        Overrides :meth:`named_buffers()` to intercept buffer names and
        remove all occurrences of the FSDP-specific flattened buffer prefix
        when inside the :meth:`summon_full_params` context manager.
        """
        should_clean_name = (
            self.training_state == TrainingState.SUMMON_FULL_PARAMS
            or self._use_orig_params
        )
        for buffer_name, buffer in super().named_buffers(*args, **kwargs):
            if should_clean_name:
                # Remove any instances of the FSDP-specific prefix; there can
                # be multiple in the case of nested FSDP modules
                buffer_name = buffer_name.replace(FSDP_PREFIX, "")
            yield (buffer_name, buffer)

    def named_parameters(
        self,
        *args,
        **kwargs,
    ) -> Iterator[Tuple[str, torch.nn.Parameter]]:
        """
        Overrides :meth:`named_parameters()` to intercept parameter names and
        remove all occurrences of the FSDP-specific flattened parameter prefix
        when inside the :meth:`summon_full_params` context manager.
        """
        should_clean_name = (
            self.training_state == TrainingState.SUMMON_FULL_PARAMS
            or self._use_orig_params
        )
        for param_name, param in super().named_parameters(*args, **kwargs):
            if should_clean_name:
                # Remove any instances of the FSDP-specific prefix; there can
                # be multiple in the case of nested FSDP modules
                param_name = param_name.replace(FSDP_PREFIX, "")
            yield (param_name, param)

    def _assert_state(self, state: Union[TrainingState, List[TrainingState]]) -> None:
        """Assert we are in the given state."""
        # Since assert can be turned off and this error checking
        # is really important, we use explicit error checking
        # and raise a ValueError if needed.
        if isinstance(state, TrainingState):
            state = [state]
        if self.training_state not in state:
            msg = (
                f"expected to be in states {state} but current state "
                f"is {self.training_state}"
            )
            # In case we are failing in the context of autograd hook, asserting
            # may not generate useful msg. So, let's print it to be sure.
            if self.rank == 0:
                print(f"Asserting FSDP instance is: {self}")
                print(f"ERROR: {msg}")
                traceback.print_stack()
            raise ValueError(msg)

    @contextmanager
    def no_sync(self) -> Generator:
        """
        A context manager to disable gradient synchronizations across FSDP
        instances. Within this context, gradients will be accumulated in module
        variables, which will later be synchronized in the first
        forward-backward pass after exiting the context. This should only be
        used on the root FSDP instance and will recursively apply to all
        children FSDP instances.

        .. note:: This likely results in higher memory usage because FSDP will
            accumulate the full model gradients (instead of gradient shards)
            until the eventual sync.

        .. note:: When used with CPU offloading, the gradients will not be
            offloaded to CPU when inside the context manager. Instead, they
            will only be offloaded right after the eventual sync.
        """
        _lazy_init(self, self)
        if not self._is_root:
            raise RuntimeError(
                "`no_sync()` on inner FSDP instances is not supported. Please call `no_sync()` on root FSDP module."
            )
        self._assert_state(TrainingState.IDLE)
        old_flags = []
        for m in self.modules():
            if isinstance(m, FullyShardedDataParallel):
                old_flags.append((m, m._sync_gradients))
                m._sync_gradients = False
        try:
            yield
        finally:
            for m, old_flag in old_flags:
                assert not m._sync_gradients, (
                    "`_sync_gradients` was incorrectly set to "
                    "`True` while in the `no_sync()` context manager"
                )
                m._sync_gradients = old_flag

    @torch.no_grad()
    def clip_grad_norm_(
        self, max_norm: Union[float, int], norm_type: Union[float, int] = 2.0
    ) -> torch.Tensor:
        """
        Clips the gradient norm of all parameters. The norm is computed over
        all parameters' gradients as viewed as a single vector, and the
        gradients are modified in-place.

        Args:
            max_norm (float or int): max norm of the gradients
            norm_type (float or int): type of the used p-norm. Can be ``'inf'``
                for infinity norm.

        Returns:
            Total norm of the parameters (viewed as a single vector).

        .. note:: If every FSDP instance uses ``NO_SHARD``, meaning that no
            gradients are sharded across ranks, then you may directly use
            :func:`torch.nn.utils.clip_grad_norm_`.

        .. note:: If at least some FSDP instance uses a sharded strategy (i.e.
            one other than ``NO_SHARD``), then you should use this method
            instead of :func:`torch.nn.utils.clip_grad_norm_` since this method
            handles the fact that gradients are sharded across ranks.

        .. note:: The total norm returned will have the "largest" dtype across
            all parameters/gradients as defined by PyTorch's type promotion
            semantics. For example, if *all* parameters/gradients use a low
            precision dtype, then the returned norm's dtype will be that low
            precision dtype, but if there exists at least one parameter/
            gradient using FP32, then the returned norm's dtype will be FP32.

        .. warning:: This needs to be called on all ranks since it uses
            collective communications.
        """
        _lazy_init(self, self)
        if not self._is_root:
            raise RuntimeError(
                "`clip_grad_norm_()` should only be called on the root FSDP instance"
            )
        self._assert_state(TrainingState.IDLE)
        # If every FSDP instance uses `NO_SHARD`, then we can directly use
        # the normal `nn.utils` one targeting local gradients
        all_no_shard = all(
            not handle.uses_sharded_strategy
            for handle in FullyShardedDataParallel._fsdp_handles(self)
        )
        if all_no_shard:
            return torch.nn.utils.clip_grad_norm_(
                self.parameters(), max_norm, norm_type
            )
        # Otherwise, there exists some FSDP instance using a sharded strategy,
        # where sharded and non-sharded parameters must be handled separately
        max_norm = float(max_norm)
        norm_type = float(norm_type)
        sharded_params = set()
        nonsharded_params = set()  # `NO_SHARD` or not FSDP-managed
        grads: List[torch.Tensor] = []
        for handle in FullyShardedDataParallel._fsdp_handles(self):
            target_set = (
                sharded_params if handle.uses_sharded_strategy else nonsharded_params
            )
            if handle._use_orig_params:
                for param in handle.flat_param._params:
                    target_set.add(param)
                    if param.grad is not None:
                        grads.append(param.grad)
            else:
                target_set.add(handle.flat_param)
                if handle.flat_param.grad is not None:
                    grads.append(handle.flat_param.grad)
        for param in self.parameters():
            not_fsdp_managed = (
                param not in sharded_params and param not in nonsharded_params
            )
            if not_fsdp_managed:
                nonsharded_params.add(param)
                if param.grad is not None:
                    grads.append(param.grad)
        # Compute local norms (forced to be in FP32)
        local_sharded_norm = _get_grad_norm(sharded_params, norm_type).to(
            self.compute_device
        )
        local_nonsharded_norm = _get_grad_norm(nonsharded_params, norm_type).to(
            self.compute_device
        )
        # Reconstruct the total gradient norm depending on the norm type
        if norm_type == math.inf:
            total_norm = torch.maximum(local_sharded_norm, local_nonsharded_norm)
            dist.all_reduce(
                total_norm, op=torch.distributed.ReduceOp.MAX, group=self.process_group
            )
        else:
            total_norm = local_sharded_norm**norm_type
            dist.all_reduce(total_norm, group=self.process_group)
            # All-reducing the local non-sharded norm would count it an extra
            # world-size-many times
            total_norm += local_nonsharded_norm**norm_type
            total_norm = total_norm ** (1.0 / norm_type)
        if self.cpu_offload.offload_params:
            total_norm = total_norm.cpu()

        clip_coef = max_norm / (total_norm + 1e-6)
        # Multiplying by the clamped coefficient is meaningless when it is
        # equal to 1, but it avoids the host-device sync that would result from
        # `if clip_coef < 1`
        clip_coef_clamped = torch.clamp(clip_coef, max=1.0)
        for grad in grads:
            grad.detach().mul_(clip_coef_clamped.to(grad.device, grad.dtype))
        # Use the "largest" dtype by type promotion semantics to use the same
        # dtype as if we did not force local norm computation to be in FP32
        total_norm_dtype = functools.reduce(
            lambda dtype1, dtype2: torch.promote_types(dtype1, dtype2),
            [grad.dtype for grad in grads],
        )
        return total_norm.to(total_norm_dtype)

    @staticmethod
    def _warn_optim_input(optim_input):
        if optim_input is not None:
            warnings.warn(
                "The `optim_input` argument is deprecated and will be removed after PyTorch 1.13. You may remove it "
                "from your code without changing its functionality."
            )

    @staticmethod
    def _is_using_optim_input(optim_input, optim) -> bool:
        if optim_input is None and optim is None:
            # Use the default behavior of `optim_input``
            return True
        if optim_input is not None:
            # Use the `optim_input` code path
            return True
        # Use the `optim` code path
        return False

    @staticmethod
    def _raise_on_use_orig_params_optim_checkpoint(
        model: nn.Module, full_optim: bool, rank0_only: bool
    ):
        if full_optim and not rank0_only:
            return
        if any(
            fsdp_module._use_orig_params
            for fsdp_module in FullyShardedDataParallel.fsdp_modules(model)
        ):
            raise NotImplementedError(
                "Optimizer state checkpointing is not supported yet for `use_orig_params=True`"
            )

    @staticmethod
    def full_optim_state_dict(
        model: torch.nn.Module,
        optim: torch.optim.Optimizer,
        optim_input: Optional[
            Union[
                List[Dict[str, Any]],
                Iterable[torch.nn.Parameter],
            ]
        ] = None,
        rank0_only: bool = True,
        group: Optional[dist.ProcessGroup] = None,
    ) -> Dict[str, Any]:
        """
        Consolidates the full optimizer state on rank 0 and returns it
        as a :class:`dict` following the convention of
        :meth:`torch.optim.Optimizer.state_dict`, i.e. with keys ``"state"``
        and ``"param_groups"``. The flattened parameters in ``FSDP`` modules
        contained in ``model`` are mapped back to their unflattened parameters.

        .. warning:: This needs to be called on all ranks since it uses
            collective communications. However, if ``rank0_only=True``, then
            the state dict is only populated on rank 0, and all other ranks
            return an empty :class:`dict`.

        .. warning:: Unlike ``torch.optim.Optimizer.state_dict()``, this method
            uses full parameter names as keys instead of parameter IDs.

        .. note:: Like in :meth:`torch.optim.Optimizer.state_dict`, the tensors
            contained in the optimizer state dict are not cloned, so there may
            be aliasing surprises. For best practices, consider saving the
            returned optimizer state dict immediately, e.g. using
            ``torch.save()``.

        Args:
            model (torch.nn.Module): Root module (which may or may not be a
                :class:`FullyShardedDataParallel` instance) whose parameters
                were passed into the optimizer ``optim``.
            optim (torch.optim.Optimizer): Optimizer for ``model`` 's
                parameters.
            optim_input (Optional[Union[List[Dict[str, Any]], Iterable[torch.nn.Parameter]]]):
                Input passed into the optimizer ``optim`` representing either a
                :class:`list` of parameter groups or an iterable of parameters;
                if ``None``, then this method assumes the input was
                ``model.parameters()``. This argument is deprecated, and there
                is no need to pass it in anymore. (Default: ``None``)
            rank0_only (bool): If ``True``, saves the populated :class:`dict`
                only on rank 0; if ``False``, saves it on all ranks. (Default:
                ``True``)
            group (dist.ProcessGroup): Model's process group or ``None`` if using
                the default process group. (Default: ``None``)

        Returns:
            Dict[str, Any]: A :class:`dict` containing the optimizer state for
            ``model`` 's original unflattened parameters and including keys
            "state" and "param_groups" following the convention of
            :meth:`torch.optim.Optimizer.state_dict`. If ``rank0_only=True``,
            then nonzero ranks return an empty :class:`dict`.
        """
        FullyShardedDataParallel._raise_on_use_orig_params_optim_checkpoint(
            model, True, rank0_only
        )
        FullyShardedDataParallel._warn_optim_input(optim_input)
        using_optim_input = FullyShardedDataParallel._is_using_optim_input(
            optim_input,
            optim,
        )
        use_orig_params: bool = False
        for module in FullyShardedDataParallel.fsdp_modules(model):
            use_orig_params = module._use_orig_params
            break
        return _optim_state_dict(
            model=model,
            optim=optim,
            optim_input=optim_input,
            rank0_only=rank0_only,
            shard_state=False,
            group=group,
            using_optim_input=using_optim_input,
            use_orig_params=use_orig_params,
        )

    @staticmethod
    def sharded_optim_state_dict(
        model: torch.nn.Module,
        optim: torch.optim.Optimizer,
        group: Optional[dist.ProcessGroup] = None,
    ) -> Dict[str, Any]:
        """
        The API is similar to :meth:`full_optim_state_dict` but this API chunks
        all non-zero-dimension states to :class:`ShardedTensor` to save memory.
        This API should only be used when the model ``state_dict`` is derived
        with the context manager ``with state_dict_type(SHARDED_STATE_DICT):``.

        For the detailed usage, refer to :meth:`full_optim_state_dict`.

        .. warning:: The returned state dict contains ``ShardedTensor`` and
            cannot be directly used by the regular ``optim.load_state_dict``.
        """
        FullyShardedDataParallel._raise_on_use_orig_params_optim_checkpoint(
            model, False, False
        )
        return _optim_state_dict(
            model=model,
            optim=optim,
            optim_input=None,
            rank0_only=False,
            shard_state=True,
            group=group,
            using_optim_input=False,
        )

    @staticmethod
    def shard_full_optim_state_dict(
        full_optim_state_dict: Dict[str, Any],
        model: torch.nn.Module,
        optim_input: Optional[
            Union[
                List[Dict[str, Any]],
                Iterable[torch.nn.Parameter],
            ]
        ] = None,
        optim: Optional[torch.optim.Optimizer] = None,
    ) -> Dict[str, Any]:
        """
        Shards the full optimizer state dict ``full_optim_state_dict`` by
        remapping the state to flattened parameters instead of unflattened
        parameters and restricting to only this rank's part of the optimizer
        state. The first argument should be the return value of
        :meth:`full_optim_state_dict`.

        Example::

            >>> # xdoctest: +SKIP("undefined variables")
            >>> from torch.distributed.fsdp import FullyShardedDataParallel as FSDP
            >>> model, optim = ...
            >>> full_osd = FSDP.full_optim_state_dict(model, optim)
            >>> torch.save(full_osd, PATH)
            >>> # Define new model with possibly different world size
            >>> new_model, new_optim = ...
            >>> full_osd = torch.load(PATH)
            >>> sharded_osd = FSDP.shard_full_optim_state_dict(full_osd, new_model)
            >>> new_optim.load_state_dict(sharded_osd)

        .. note:: Both :meth:`shard_full_optim_state_dict` and
            :meth:`scatter_full_optim_state_dict` may be used to get the
            sharded optimizer state dict to load. Assuming that the full
            optimizer state dict resides in CPU memory, the former requires
            each rank to have the full dict in CPU memory, where each rank
            individually shards the dict without any communication, while the
            latter requires only rank 0 to have the full dict in CPU memory,
            where rank 0 moves each shard to GPU memory (for NCCL) and
            communicates it to ranks appropriately. Hence, the former has
            higher aggregate CPU memory cost, while the latter has higher
            communication cost.

        Args:
            full_optim_state_dict (Dict[str, Any]): Optimizer state dict
                corresponding to the unflattened parameters and holding the
                full non-sharded optimizer state.
            model (torch.nn.Module): Root module (which may or may not be a
                :class:`FullyShardedDataParallel` instance) whose parameters
                correspond to the optimizer state in ``full_optim_state_dict``.
            optim_input (Optional[Union[List[Dict[str, Any]], Iterable[torch.nn.Parameter]]]):
                Input passed into the optimizer representing either a
                :class:`list` of parameter groups or an iterable of parameters;
                if ``None``, then this method assumes the input was
                ``model.parameters()``. This argument is deprecated, and there
                is no need to pass it in anymore. (Default: ``None``)
            optim (Optional[torch.optim.Optimizer]): Optimizer that will load
                the state dict returned by this method. This is the preferred
                argument to use over ``optim_input``. (Default: ``None``)

        Returns:
            Dict[str, Any]: The full optimizer state dict now remapped to
            flattened parameters instead of unflattened parameters and
            restricted to only include this rank's part of the optimizer state.
        """
        FullyShardedDataParallel._raise_on_use_orig_params_optim_checkpoint(
            model, True, False
        )
        FullyShardedDataParallel._warn_optim_input(optim_input)
        using_optim_input = FullyShardedDataParallel._is_using_optim_input(
            optim_input,
            optim,
        )
        use_orig_params: bool = False
        for module in FullyShardedDataParallel.fsdp_modules(model):
            use_orig_params = module._use_orig_params
            break
        sharded_osd = _flatten_optim_state_dict(
            full_optim_state_dict,
            model,
            True,
            use_orig_params,
        )
        return _rekey_sharded_optim_state_dict(
            sharded_osd,
            model,
            optim,
            optim_input,
            using_optim_input,
        )

    @staticmethod
    def flatten_sharded_optim_state_dict(
        sharded_optim_state_dict: Dict[str, Any],
        model: torch.nn.Module,
        optim: Optional[torch.optim.Optimizer] = None,
    ) -> Dict[str, Any]:
        """
        The API is similar to :meth:`shard_full_optim_state_dict`. The only
        difference is that the input ``sharded_optim_state_dict`` should be
        returned from :meth:`sharded_optim_state_dict`. Therefore, there will
        be all-gather calls on each rank to gather ``ShardedTensor`` s.

        Args:
            sharded_optim_state_dict (Dict[str, Any]): Optimizer state dict
                corresponding to the unflattened parameters and holding the
                sharded optimizer state.
            model (torch.nn.Module):
                Refer to :meth:``shard_full_optim_state_dict``.

        Returns:
            Refer to :meth:`shard_full_optim_state_dict`.
        """
        FullyShardedDataParallel._raise_on_use_orig_params_optim_checkpoint(
            model, True, True
        )
        # TODO: The implementation is the same as ``shard_full_optim_state_dict``.
        # See the TODO in ``shard_full_optim_state_dict`` for the future
        # unification plan.
        flattened_osd = _flatten_optim_state_dict(
            sharded_optim_state_dict,
            model=model,
            shard_state=True,
        )
        return _rekey_sharded_optim_state_dict(
            flattened_osd,
            model,
            optim,
            None,
            False,
        )

    @staticmethod
    def scatter_full_optim_state_dict(
        full_optim_state_dict: Optional[Dict[str, Any]],
        model: torch.nn.Module,
        optim_input: Optional[
            Union[
                List[Dict[str, Any]],
                Iterable[torch.nn.Parameter],
            ]
        ] = None,
        optim: Optional[torch.optim.Optimizer] = None,
        group: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """
        Scatters the full optimizer state dict from rank 0 to all other ranks,
        returning the sharded optimizer state dict on each rank. The return
        value is the same as :meth:`shard_full_optim_state_dict`, and on rank
        0, the first argument should be the return value of
        :meth:`full_optim_state_dict`.

        Example::

            >>> # xdoctest: +SKIP("undefined variables")
            >>> from torch.distributed.fsdp import FullyShardedDataParallel as FSDP
            >>> model, optim = ...
            >>> full_osd = FSDP.full_optim_state_dict(model, optim)  # only non-empty on rank 0
            >>> # Define new model with possibly different world size
            >>> new_model, new_optim, new_group = ...
            >>> sharded_osd = FSDP.scatter_full_optim_state_dict(full_osd, new_model, group=new_group)
            >>> new_optim.load_state_dict(sharded_osd)

        .. note:: Both :meth:`shard_full_optim_state_dict` and
            :meth:`scatter_full_optim_state_dict` may be used to get the
            sharded optimizer state dict to load. Assuming that the full
            optimizer state dict resides in CPU memory, the former requires
            each rank to have the full dict in CPU memory, where each rank
            individually shards the dict without any communication, while the
            latter requires only rank 0 to have the full dict in CPU memory,
            where rank 0 moves each shard to GPU memory (for NCCL) and
            communicates it to ranks appropriately. Hence, the former has
            higher aggregate CPU memory cost, while the latter has higher
            communication cost.

        Args:
            full_optim_state_dict (Optional[Dict[str, Any]]): Optimizer state
                dict corresponding to the unflattened parameters and holding
                the full non-sharded optimizer state if on rank 0; the argument
                is ignored on nonzero ranks.
            model (torch.nn.Module): Root module (which may or may not be a
                :class:`FullyShardedDataParallel` instance) whose parameters
                correspond to the optimizer state in ``full_optim_state_dict``.
            optim_input (Optional[Union[List[Dict[str, Any]], Iterable[torch.nn.Parameter]]]):
                Input passed into the optimizer representing either a
                :class:`list` of parameter groups or an iterable of parameters;
                if ``None``, then this method assumes the input was
                ``model.parameters()``. This argument is deprecated, and there
                is no need to pass it in anymore. (Default: ``None``)
            optim (Optional[torch.optim.Optimizer]): Optimizer that will load
                the state dict returned by this method. This is the preferred
                argument to use over ``optim_input``. (Default: ``None``)
            group (dist.ProcessGroup): Model's process group or ``None`` if
                using the default process group. (Default: ``None``)

        Returns:
            Dict[str, Any]: The full optimizer state dict now remapped to
            flattened parameters instead of unflattened parameters and
            restricted to only include this rank's part of the optimizer state.
        """
        FullyShardedDataParallel._raise_on_use_orig_params_optim_checkpoint(
            model, True, True
        )
        FullyShardedDataParallel._warn_optim_input(optim_input)
        using_optim_input = FullyShardedDataParallel._is_using_optim_input(
            optim_input,
            optim,
        )
        # Try to use the passed-in process group, the model's process group,
        # or the default process group (i.e. `None`) in that priority order
        if group is None and hasattr(model, "process_group"):
            group = model.process_group
        rank = dist.get_rank(group)
        world_size = dist.get_world_size(group)
        # Check for a valid broadcast device, preferring GPU when available
        using_nccl = dist.distributed_c10d._check_for_nccl_backend(group)
        broadcast_device = (
            torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
        )
        if using_nccl and not torch.cuda.is_available():
            raise RuntimeError("NCCL requires a GPU for collectives")
        # Flatten the optimizer state dict and construct a copy with the
        # positive-dimension tensors' shapes in place of the tensors themselves
        # since those tensors will be broadcast separately to avoid copying
        if rank == 0:
            if full_optim_state_dict is None:
                raise ValueError("Rank 0 must pass in the full optimizer state dict")
            flat_osd = _flatten_optim_state_dict(
                full_optim_state_dict,
                model=model,
                shard_state=False,
            )
            processed_osd = _process_pos_dim_tensor_state(flat_osd, world_size)
        # Broadcast the optim state dict without positive-dimension tensor
        # state and the FSDP parameter IDs from rank 0 to all ranks
        processed_osd = _broadcast_processed_optim_state_dict(
            processed_osd if rank == 0 else None,
            rank,
            group,
        )
        # Broadcast positive-dimension tensor state (both sharded tensors for
        # FSDP parameters and unsharded tensors for non-FSDP parameters)
        sharded_osd = _broadcast_pos_dim_tensor_states(
            processed_osd,
            flat_osd if rank == 0 else None,
            rank,
            world_size,
            group,
            broadcast_device,
        )
        # Rekey the optimizer state dict to use parameter IDs according to this
        # rank's `optim`
        sharded_osd = _rekey_sharded_optim_state_dict(
            sharded_osd,
            model,
            optim,
            optim_input,
            using_optim_input,
        )
        return sharded_osd

    @staticmethod
    def rekey_optim_state_dict(
        optim_state_dict: Dict[str, Any],
        optim_state_key_type: OptimStateKeyType,
        model: torch.nn.Module,
        optim_input: Optional[
            Union[
                List[Dict[str, Any]],
                Iterable[torch.nn.Parameter],
            ]
        ] = None,
        optim: Optional[torch.optim.Optimizer] = None,
    ) -> Dict[str, Any]:
        """
        Re-keys the optimizer state dict ``optim_state_dict`` to use the key
        type ``optim_state_key_type``. This can be used to achieve
        compatibility between optimizer state dicts from models with FSDP
        instances and ones without.

        To re-key an FSDP full optimizer state dict (i.e. from
        :meth:`full_optim_state_dict`) to use parameter IDs and be loadable to
        a non-wrapped model::

            >>> # xdoctest: +SKIP("undefined variables")
            >>> wrapped_model, wrapped_optim = ...
            >>> full_osd = FSDP.full_optim_state_dict(wrapped_model, wrapped_optim)
            >>> nonwrapped_model, nonwrapped_optim = ...
            >>> rekeyed_osd = FSDP.rekey_optim_state_dict(full_osd, OptimStateKeyType.PARAM_ID, nonwrapped_model)
            >>> nonwrapped_optim.load_state_dict(rekeyed_osd)

        To re-key a normal optimizer state dict from a non-wrapped model to be
        loadable to a wrapped model::

            >>> # xdoctest: +SKIP("undefined variables")
            >>> nonwrapped_model, nonwrapped_optim = ...
            >>> osd = nonwrapped_optim.state_dict()
            >>> rekeyed_osd = FSDP.rekey_optim_state_dict(osd, OptimStateKeyType.PARAM_NAME, nonwrapped_model)
            >>> wrapped_model, wrapped_optim = ...
            >>> sharded_osd = FSDP.shard_full_optim_state_dict(rekeyed_osd, wrapped_model)
            >>> wrapped_optim.load_state_dict(sharded_osd)

        Returns:
            Dict[str, Any]: The optimizer state dict re-keyed using the
            parameter keys specified by ``optim_state_key_type``.
        """
        FullyShardedDataParallel._warn_optim_input(optim_input)
        using_optim_input = FullyShardedDataParallel._is_using_optim_input(
            optim_input,
            optim,
        )
        assert optim_state_key_type in (
            OptimStateKeyType.PARAM_NAME,
            OptimStateKeyType.PARAM_ID,
        )
        osd = optim_state_dict  # alias
        # Validate that the existing parameter keys are uniformly typed
        uses_param_name_mask = [type(param_key) is str for param_key in osd["state"]]
        uses_param_id_mask = [type(param_key) is int for param_key in osd["state"]]
        if (any(uses_param_name_mask) and not all(uses_param_name_mask)) or (
            any(uses_param_id_mask) and not all(uses_param_id_mask)
        ):
            error_msg = f"Invalid parameter keys: {osd['state'].keys()}"
            raise ValueError(error_msg)
        # Return directly if the existing key type matches the target key type
        if (
            optim_state_key_type == OptimStateKeyType.PARAM_NAME
            and all(uses_param_name_mask)
        ) or (
            optim_state_key_type == OptimStateKeyType.PARAM_ID
            and all(uses_param_id_mask)
        ):
            return osd
        # Otherwise, actually perform the re-keying
        new_osd = {}
        if optim_state_key_type == OptimStateKeyType.PARAM_NAME:  # ID -> name
            param_id_to_param = (
                _get_param_id_to_param_from_optim_input(model, optim_input)
                if using_optim_input
                else _get_param_id_to_param(optim)
            )
            param_to_param_name = _get_param_to_fqn(model)
            param_id_to_param_name: List[str] = [
                param_to_param_name[param] for param in param_id_to_param
            ]
            new_osd["state"] = {
                param_id_to_param_name[param_id]: param_state
                for param_id, param_state in osd["state"].items()
            }
            new_osd["param_groups"] = copy.deepcopy(osd["param_groups"])
            for param_group in new_osd["param_groups"]:
                param_group["params"] = sorted(
                    [
                        param_id_to_param_name[param_id]
                        for param_id in param_group["params"]
                    ]
                )
            return new_osd
        elif optim_state_key_type == OptimStateKeyType.PARAM_ID:  # name -> ID
            param_name_to_param = _get_fqn_to_param(model)
            param_to_param_id = (
                _get_param_to_param_id_from_optim_input(model, optim_input)
                if using_optim_input
                else _get_param_to_param_id(optim)
            )
            # Because not all model parameters may be passed as the optimizer
            # input, we may need to drop some parameters from this mapping
            param_name_to_param_id = {
                param_name: param_to_param_id[param]
                for param_name, param in param_name_to_param.items()
                if param in param_to_param_id
            }
            new_osd["state"] = {
                param_name_to_param_id[param_name]: param_state
                for param_name, param_state in osd["state"].items()
            }
            new_osd["param_groups"] = copy.deepcopy(osd["param_groups"])
            for param_group in new_osd["param_groups"]:
                param_group["params"] = sorted(
                    [
                        param_name_to_param_id[param_name]
                        for param_name in param_group["params"]
                    ]
                )
            return new_osd
        return new_osd  # should never reach here

    @staticmethod
    def _optim_state_dict(
        model: torch.nn.Module,
        optim: torch.optim.Optimizer,
        group: Optional[dist.ProcessGroup] = None,
    ) -> Dict[str, Any]:
        """
        This API is still being developed, hence the `_` prefix. The comment
        below is also not fully implemented yet. Do not use this API unless
        you know why this API exists and how this API works.

        Returns the optimizer state. The state will be sharded or consolidated
        based on ``state_dict_type`` set by :meth:`set_state_dict_type` or
        :meth:`state_dict_type`.

        Args:
            model (torch.nn.Module): Root module (which may or may not be a
                :class:`FullyShardedDataParallel` instance) whose parameters
                were passed into the optimizer ``optim``.
            optim (torch.optim.Optimizer): Optimizer for ``model`` 's
                parameters.
            group (dist.ProcessGroup): Model's process group across which parameters
                are sharded or ``None`` if using the default process group. (
                Default: ``None``)

        Returns:
            Dict[str, Any]: A :class:`dict` containing the optimizer state for
            ``model``. The sharding of the optimizer state is based on
            ``state_dict_type``.
        """
        return FullyShardedDataParallel.full_optim_state_dict(
            model, optim, rank0_only=False, group=group
        )

    @staticmethod
    def _load_optim_state_dict(
        state_dict: Dict[str, Any],
        model: torch.nn.Module,
        optim: torch.optim.Optimizer,
        group: Optional[dist.ProcessGroup] = None,
    ) -> Dict[str, Any]:
        """
        This API is still being developed, hence the `_` prefix. The comment
        below is also not fully implemented yet. Do not use this API unless
        you know why this API exists and how this API works.

        Load the optimizer state, ``state_dict``, to the optimizer ``optim``.
        ``state_dict_type`` set by :meth:``set_state_dict_type`` decides how
        to load the state_dict.

        Args:
            state_dict (Dict[str, Any]): The optimizer states to be loaded.
            model (torch.nn.Module): Root module (which may or may not be a
                :class:`FullyShardedDataParallel` instance) whose parameters
                were passed into the optimizer ``optim``.
            optim (torch.optim.Optimizer): Optimizer for ``model`` 's
                parameters.
            group (dist.ProcessGroup): Model's process group across which parameters
                are sharded or ``None`` if using the default process group. (
                Default: ``None``)
        """
        optim_state_dict = FullyShardedDataParallel.shard_full_optim_state_dict(
            state_dict, model=model, optim=optim
        )
        return optim_state_dict

    def register_comm_hook(self, state: object, hook: callable):
        """
        Registers a communication hook which is an enhancement that provides a
        flexible hook to users where they can specify how FSDP aggregates gradients
        across multiple workers.
        This hook can be used to implement several algorithms like
        `GossipGrad <https://arxiv.org/abs/1803.05880>`_ and gradient compression
        which involve different communication strategies for
        parameter syncs while training with :class:`FullyShardedDataParallel`.

        .. warning ::
            FSDP communication hook should be registered before running an initial forward pass
            and only once.

        Args:
            state (object): Passed to the hook to maintain any state information during the training process.
                            Examples include error feedback in gradient compression,
                            peers to communicate with next in `GossipGrad <https://arxiv.org/abs/1803.05880>`_, etc.
                            It is locally stored by each worker
                            and shared by all the gradient tensors on the worker.
            hook (Callable): Callable, which has one of the following signatures:
                            1) ``hook: Callable[torch.Tensor] -> None``:
                            This function takes in a Python tensor, which represents
                            the full, flattened, unsharded gradient with respect to all variables
                            corresponding to the model this FSDP unit is wrapping
                            (that are not wrapped by other FSDP sub-units).
                            It then performs all necessary processing and returns ``None``;
                            2) ``hook: Callable[torch.Tensor, torch.Tensor] -> None``:
                            This function takes in two Python tensors, the first one represents
                            the full, flattened, unsharded gradient with respect to all variables
                            corresponding to the model this FSDP unit is wrapping
                            (that are not wrapped by other FSDP sub-units). The latter
                            represents a pre-sized tensor to store a chunk of a sharded gradient after
                            reduction.
                            In both cases, callable performs all necessary processing and returns ``None``.
                            Callables with signature 1 are expected to handle gradient communication for a `NO_SHARD` case.
                            Callables with signature 2 are expected to handle gradient communication for sharded cases.

        """
        if not self.check_is_root():
            raise AssertionError(
                "register_comm_hook can only be called on a root instance."
            )
        for submodule in self.fsdp_modules(self):
            assert (
                not submodule._hook_registered
            ), "communication hook can be only registered once"
            submodule._hook_registered = True
            assert submodule._communication_hook == _get_default_comm_hook(
                self.sharding_strategy
            ), f"communication hook should be default, but it is {submodule._communication_hook.__name__} instead"
            submodule._communication_hook_state = state
            submodule._communication_hook = hook


def _get_grad_norm(
    params: Iterable[nn.Parameter],
    norm_type: float,
) -> torch.Tensor:
    """
    Returns the gradient norm of parameters ``param`` s, where the gradients
    are viewed as a single vector. The returned norm is in FP32 even if
    parameters/gradients are in a low precision. This is because the downstream
    use of this return value is a reduction across ranks.
    """
    params_with_grad = [param for param in params if param.grad is not None]
    if len(params_with_grad) == 0:
        return torch.tensor(0.0)
    grads = [param.grad for param in params_with_grad]
    grad_dtypes = set(grad.dtype for grad in grads)
    if len(grad_dtypes) != 1:
        raise ValueError(
            f"Requires uniform dtype across all gradients but got {grad_dtypes}"
        )
    # Compute the gradient norm in FP32, where we treat the gradients as a
    # single vector
    grad_norm = torch.linalg.vector_norm(
        torch.stack(
            [
                torch.linalg.vector_norm(grad.detach(), norm_type, dtype=torch.float32)
                for grad in grads
            ],
        ),
        norm_type,
        dtype=torch.float32,
    )
    return grad_norm


def _get_param_to_fqn(
    model: torch.nn.Module,
) -> Dict[torch.nn.Parameter, str]:
    """
    Constructs a mapping from parameters to their parameter names. ``model``
    should not contain any :class:`FullyShardedDataParallel` instances, which
    means that none of the parameters should be ``FlatParameter`` s. As a
    result, compared to :meth:`_get_param_to_fqns`, the mapped
    values may be flattened from singleton :class:`list` s to the contained
    names themselves.

    Args:
        model (torch.nn.Module): Root module, which should not contain any
            :class:`FullyShardedDataParallel` instances.
    """
    param_to_param_names = _get_param_to_fqns(model)
    for param_names in param_to_param_names.values():
        assert len(param_names) > 0, (
            "`_get_param_to_fqns()` " "should not construct empty lists"
        )
        if len(param_names) > 1:
            raise RuntimeError(
                "Each parameter should only map to one parameter name but got "
                f"{len(param_names)}: {param_names}"
            )
    param_to_param_name = {
        param: param_names[0] for param, param_names in param_to_param_names.items()
    }
    return param_to_param_name


def _get_fqn_to_param(
    model: torch.nn.Module,
) -> Dict[str, torch.nn.Parameter]:
    """Constructs the inverse mapping of :meth:`_get_param_to_fqn`."""
    param_to_param_name = _get_param_to_fqn(model)
    return dict(zip(param_to_param_name.values(), param_to_param_name.keys()))
