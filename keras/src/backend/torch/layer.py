from typing import Iterator
from typing import Tuple

import torch

from keras.src.backend.common.stateless_scope import in_stateless_scope
from keras.src.ops.operation import Operation

# NEW_IMPL=False
NEW_IMPL = True


class TorchLayer(torch.nn.Module):
    """Adaptation layer to make sure keras.layers.Layer works well with
    torch.nn.Module. Currently, the main modification are on parameter/module
    tracking and pointing torch.nn.Module.forward() to the right keras call.

    Module tracking:
      All sublayers are tracked as modules in Module._modules. All module level
    api with recurse=True should work properly just like a torch.nn.Module.

    Variable tracking:
      Since keras has a different variable tracking mechanism, unlike modules,
    Modules._parameter doesn't automatically tracks variables created for torch
    layers.
      This is currently manually populated through _track_torch_params() that
    does following work:
        1. Populate all sublayers torch params by calling _track_torch_params()
        2. Create a single torch.nn.ParameterList() parameter with trainable,
           non trainable and seed generator states belongs to the current layer.
      Since keras also allows untrack / track object post build, eg.
    Dense.enable_lora(), Dense.quantization(); _untrack_torch_params() is added
    that allows refresh the parameters expose to torch module. A re-populate
    will trigger every time when Layer.track_variable() and
    Layer._untrack_variable() is called.

    Few additional points that user should be aware of:
    1. When torch backend is enabled KerasVariable.value is torch.nn.Parameter,
       this is not visible to torch since it is separately tracked in keras
       tracker.
    2. When torch parameter is exposed with _track_torch_params(), no copy is
       made to the torch parameter in keras tracker; so both keras tracker and
       torch module sees the same object it is just present in 2 different
       member variables. This also means any modification to keras variable,
       for instance, setting trainable is automatically populated to torch
       parameters.
    3. Since keras creates variables in a deterministic order, resulted torch
       parameter list will also in deterministic order with the order of
       trainable->non_trainable->seed_generator_states. Changing variable from
       trainable to non trainable won't move keras variable from one tracker to
       the another, so does the final populated torch_params.
    4. It is recommended for user to alternate variables through keras variable
       apis instead of alternate with torch_params since it is simpler with the
       keras variable api and it is backend agnostic.
    5. Any torch module operation should in theory works; for example
       state_dict() and load_state_dict() works if you want a more torch way of
       saving variables.
    6. Although not recommended, but you can use below code snippet to find the
       corresponding parameter in torch_params from a keras variable:
       parameters = [(pname, p) for pname, p in layer.named_parameters() \
                      if id(p) == id(variable.value)]
    """

    def _post_build(self):
        # Do not track variables when in a stateless scope.
        # The variables are not initialized.
        if in_stateless_scope():
            return
        self._track_torch_params()

    def _track_torch_params(self):
        if not NEW_IMPL:
            self.torch_params = torch.nn.ParameterDict(
                {variable.path: variable.value for variable in self.variables}
            )
            return
        for layer in self._layers:
            layer._track_torch_params()
        if self._torch_params_tracked():
            return
        torch_params = []
        for v in self._trainable_variables + self._non_trainable_variables:
            torch_params.append(v.value)
        for sg in self._seed_generators:
            torch_params.append(sg.state.value)

        # set torch_params attribute will have module automatically track
        # parameters.
        self.torch_params = torch.nn.ParameterList(torch_params)

    def _untrack_torch_params(self):
        for layer in self._layers:
            layer._untrack_torch_params()
        del self.torch_params

    def _torch_params_tracked(self):
        return hasattr(self, "torch_params")

    def named_parameters(
        self,
        prefix: str = "",
        recurse: bool = True,
        remove_duplicate: bool = True,
    ) -> Iterator[Tuple[str, torch.nn.Parameter]]:
        if not self._torch_params_tracked():
            if self.built:
                self._track_torch_params()
            else:
                raise RuntimeError(
                    "Torch parameters are not tracked yet and layer is not "
                    "built. Did you forget to call build()?"
                )
        return torch.nn.Module.named_parameters(
            self, prefix, recurse, remove_duplicate
        )

    def forward(self, *args, **kwargs):
        return Operation.__call__(self, *args, **kwargs)

    def _setattr_hook(self, name, value):
        from keras.src.layers import Layer

        if (
            isinstance(value, torch.nn.Module)
            and not isinstance(value, Layer)
            and not name == "torch_params"
        ):
            from keras.src.utils.torch_utils import TorchModuleWrapper

            if not isinstance(self, TorchModuleWrapper):
                value = TorchModuleWrapper(value)
        # Torch module don't register list[Module] in its __setattr__, it uses
        # nn.ModuleList normally. In Keras3, we only need a way for the module
        # class to be tracked by torch since keras3 user can still do
        # self._layers to reference all layers instead of using
        # torch.nn.Module.named_members().
        # if isinstance(value, list) and all(
        #    [isinstance(v, Layer) for v in value]
        # ):
        #    for idx, v in enumerate(value):
        #        self.add_module(f"torch_module_{name}_{idx}", v)
        return name, value

    def _post_track_variable(self, variable):
        if self._torch_params_tracked():
            if not NEW_IMPL:
                self.torch_params[variable.path] = variable.value
                return
            self._untrack_torch_params()
            self._track_torch_params()

    def _post_untrack_variable(self, variable):
        if self._torch_params_tracked():
            if not NEW_IMPL:
                self.torch_params[variable.path] = variable.value
                return
            self._untrack_torch_params()
            self._track_torch_params()
