import os
import tempfile
from string import Template
import copy
import unittest
import warnings
import inspect

import torch
from torch._six import PY2
import common_utils as common
import common_nn
from common_cuda import TEST_CUDA
import torch.utils.cpp_extension
from cpp_api_parity import sample_module, torch_nn_modules, TorchNNTestParams, CppArg, parse_parity_tracker_table


parity_table_path = os.path.join(os.path.dirname(__file__), 'cpp_api_parity/parity-tracker.md')

parity_table = parse_parity_tracker_table(parity_table_path)

TORCH_NN_MODULE_COMMON_TEST_HARNESS = """\n
#include <torch/script.h>

const char * const parity_test_error_msg_prefix = "Parity test failed: ";

#define GENERATE_PARITY_TEST_ERROR_MSG(name, cpp_value, python_value) \
  parity_test_error_msg_prefix, \
  name, " in C++ has value: ", cpp_value, ", which does not match the corresponding value in Python: ", python_value \

bool check_tensor_equality(const torch::Tensor& tensor1, const torch::Tensor& tensor2) {
  return tensor1.sizes().vec() == tensor2.sizes().vec() && \
    tensor1.device() == tensor2.device() && \
    tensor1.dtype() == tensor2.dtype() && \
    tensor1.allclose(tensor2);
}

bool check_ivalue_equality(const c10::IValue& ivalue1, const c10::IValue& ivalue2) {
  if (ivalue1.tagKind() != ivalue2.tagKind()) {
    AT_ERROR("Value type mismatch: ", "ivalue1: ", ivalue1.tagKind(), ", ivalue2: ", ivalue2.tagKind());
  }
  if (ivalue1.isInt()) {
    return ivalue1.toInt() == ivalue2.toInt();
  } else if (ivalue1.isDouble()) {
    return ivalue1.toDouble() == ivalue2.toDouble();
  } else if (ivalue1.isBool()) {
    return ivalue1.toBool() == ivalue2.toBool();
  } else if (ivalue1.isString()) {
    return ivalue1.toString() == ivalue2.toString();
  } else if (ivalue1.isTensor()) {
    return check_tensor_equality(ivalue1.toTensor(), ivalue2.toTensor());
  } else {
    AT_ERROR("Unsupported value type: ", ivalue1.tagKind());
  }
}
"""

CHECK_MODULE_PARAM_EQUALITY = Template("""\
TORCH_CHECK(
  check_tensor_equality(${script_module_prefix}.get_parameter("${param_name}"), ${cpp_module_prefix}->${param_name}),
  GENERATE_PARITY_TEST_ERROR_MSG(
    "`${cpp_module_prefix}->${param_name}`",
    ${cpp_module_prefix}->${param_name},
    ${script_module_prefix}.get_parameter("${param_name}")));
TORCH_CHECK(
  ${script_module_prefix}.get_parameter("${param_name}").requires_grad() == ${cpp_module_prefix}->${param_name}.requires_grad(),
  GENERATE_PARITY_TEST_ERROR_MSG(
    "`${cpp_module_prefix}->${param_name}.requires_grad()`",
    ${cpp_module_prefix}->${param_name}.requires_grad(),
    ${script_module_prefix}.get_parameter("${param_name}").requires_grad()));
""")

CHECK_MODULE_BUFFER_EQUALITY = Template("""\
TORCH_CHECK(
  check_tensor_equality(${script_module_prefix}.get_buffer("${buffer_name}"), ${cpp_module_prefix}->${buffer_name}),
  GENERATE_PARITY_TEST_ERROR_MSG(
    "`${cpp_module_prefix}->${buffer_name}`",
    ${cpp_module_prefix}->${buffer_name},
    ${script_module_prefix}.get_buffer("${buffer_name}")));
""")

CHECK_MODULE_ATTR_EQUALITY = Template("""\
TORCH_CHECK(
  check_ivalue_equality(
    ${script_module_prefix}.get_attribute("${attr_name}"), c10::IValue(${cpp_module_prefix}->${attr_name})),
  GENERATE_PARITY_TEST_ERROR_MSG(
    "`${cpp_module_prefix}->${attr_name}`",
    ${cpp_module_prefix}->${attr_name},
    ${script_module_prefix}.get_attribute("${attr_name}")));
""")

TORCH_NN_MODULE_TEST_CTOR_ARGS = Template("""\n
void ${module_name}_test_ctor_args() {
  ${module_qualified_name} m_init_by_cpp(${module_option});
}
""")

TORCH_NN_MODULE_TEST_INIT = Template("""\n
void ${module_variant_name}_test_init(
    const std::string& saved_module_path,
    const std::string& device) {
  torch::jit::script::Module m_init_by_python = torch::jit::load(saved_module_path);

  torch::manual_seed(2);
  ${module_qualified_name} m_init_by_cpp${cpp_constructor_args};
  m_init_by_cpp->to(device);

  ${extra_stmts}
}
""")

TORCH_NN_MODULE_TEST_FORWARD = Template("""\n
void ${module_variant_name}_test_forward(
    const std::string& saved_module_path,
    const std::string& device,
    torch::Tensor python_output,
    ${input_arg_declarations}) {
  torch::manual_seed(2);
  ${module_qualified_name} module${cpp_constructor_args};
  torch::load(module, saved_module_path);
  module->to(device);

  auto cpp_output = module(${input_args});

  TORCH_CHECK(
    check_tensor_equality(cpp_output, python_output),
    GENERATE_PARITY_TEST_ERROR_MSG(
      "forward output",
      cpp_output,
      python_output));

  ${extra_stmts}
}
""")

TORCH_NN_MODULE_TEST_BACKWARD = Template("""\n
void ${module_variant_name}_test_backward(
    const std::string& saved_module_path,
    const std::string& saved_grad_module_path,
    const std::string& device,
    ${input_arg_declarations}) {
  ${module_qualified_name} python_grad_module${cpp_constructor_args};
  torch::load(python_grad_module, saved_grad_module_path);

  torch::manual_seed(2);
  ${module_qualified_name} module${cpp_constructor_args};
  torch::load(module, saved_module_path);
  module->to(device);

  auto cpp_output = module(${input_args});
  cpp_output.sum().backward();

  for (size_t i = 0; i < module->parameters().size(); i++) {
    auto named_param = module->named_parameters()[i];
    auto grad = python_grad_module->parameters()[i];
    TORCH_CHECK(
      check_tensor_equality(named_param->grad(), grad),
      GENERATE_PARITY_TEST_ERROR_MSG(
        "gradient of `" + named_param.key() + "`",
        named_param->grad(),
        grad));
  }

  ${extra_stmts}
}
""")

TORCH_NN_MODULE_IGNORED_ATTRS = {
    '_backend', '_parameters', '_buffers', '_backward_hooks', '_forward_hooks', '_forward_pre_hooks',
    '_state_dict_hooks', '_load_state_dict_pre_hooks', '_modules', 'training', 'has_parity',
}

class TestCppApiParity(common.TestCase):
    def _python_arg_to_cpp_arg(self, python_arg):
        if type(python_arg) == int:
            return CppArg(type='int64_t', value=str(python_arg))
        elif type(python_arg) == float:
            return CppArg(type='double', value=str(python_arg))
        elif type(python_arg) == bool:
            return CppArg(type='bool', value=str(python_arg).lower())
        elif type(python_arg) == str:
            return CppArg(type='std::string', value='"{}"'.format(python_arg))
        elif type(python_arg) == torch.Tensor:
            return CppArg(
                type='torch::Tensor',
                value='torch::empty({})'.format(str(list(python_arg.shape)).replace('[', '{').replace(']', '}')))
        else:
            raise RuntimeError(
                "{} is not a supported arg type for C++ module methods".format(type(python_default_value)))

    def _compile_cpp_code_inline(self, name, cpp_sources, functions):
        # Just-in-time compile the C++ test code
        cpp_module = torch.utils.cpp_extension.load_inline(
            name=name,
            cpp_sources=cpp_sources,
            functions=functions,
            verbose=False,
        )
        return cpp_module

    # This tests that Python and C++ torch.nn modules have matching constructor arg names and types.
    def _test_torch_nn_module_ctor_args(self, module_name):
        python_module_class = getattr(torch.nn, module_name)
        module_metadata = torch_nn_modules.module_metadata_map[module_name]
        cpp_default_constructor_args_str = module_metadata.cpp_default_constructor_args
        if PY2:
            init_arg_spec = inspect.getargspec(python_module_class.__init__)
        else:
            init_arg_spec = inspect.getfullargspec(python_module_class.__init__)
        init_kwargs_defaults = init_arg_spec.defaults
        python_default_constructor_arg_names = [x for x in init_arg_spec.args[1:-len(init_kwargs_defaults)] if x != 'has_parity']
        cpp_default_constructor_arg_values = cpp_default_constructor_args_str.strip('()').split(',')
        self.assertEqual(
            len(cpp_default_constructor_arg_values),
            len(python_default_constructor_arg_names),
            "The constructor of `torch::nn::{}` in C++ ".format(module_name) +
            "must take the exact same number of non-keyword arguments " +
            "as the constructor of `torch.nn.{}` in Python. ".format(module_name) +
            "However, currently the C++ constructor expects {} non-keyword argument(s) ".format(
                len(cpp_default_constructor_arg_values)) +
            "while the Python constructor expects {} non-keyword argument(s): {}".format(
                len(python_default_constructor_arg_names),
                python_default_constructor_arg_names))

        cpp_module_option = 'torch::nn::{}Options{}'.format(module_name, cpp_default_constructor_args_str)
        init_kwargs = init_arg_spec.args[-len(init_kwargs_defaults):]
        for arg_name, python_default_value in zip(init_kwargs, init_kwargs_defaults):
            cpp_module_option += '.{}({})'.format(arg_name, self._python_arg_to_cpp_arg(python_default_value).value)

        cpp_sources = TORCH_NN_MODULE_COMMON_TEST_HARNESS + module_metadata.cpp_sources
        cpp_sources += TORCH_NN_MODULE_TEST_CTOR_ARGS.substitute(
            module_name=module_name,
            module_qualified_name='torch::nn::{}'.format(module_name),
            module_option=cpp_module_option)
        cpp_test_name = module_name + '_test_ctor_args'
        cpp_module = self._compile_cpp_code_inline(
            name=cpp_test_name, cpp_sources=cpp_sources, functions=cpp_test_name)

        getattr(cpp_module, cpp_test_name)()

    def _test_torch_nn_module_variant(self, test_params):
        def generate_test_cpp_sources(test_params, template, extra_stmts):
            example_inputs = test_params.example_inputs
            input_arg_types = [self._python_arg_to_cpp_arg(arg).type for arg in example_inputs]
            input_args = ['arg{}'.format(str(i)) for i in range(len(input_arg_types))]
            input_arg_declarations = ['{} {}'.format(arg_type, arg_name) for arg_type, arg_name in zip(input_arg_types, input_args)]
            test_cpp_sources = template.substitute(
                module_variant_name=test_params.module_variant_name,
                module_qualified_name='torch::nn::{}'.format(test_params.module_name),
                cpp_constructor_args=test_params.cpp_constructor_args,
                input_arg_declarations=',\n'.join(input_arg_declarations),
                input_args=',\n'.join(input_args),
                extra_stmts=extra_stmts)
            return test_cpp_sources

        def setup_init_test(test_params):
            # We are generating the attribute equality checks manually here,
            # because it is not possible to have a `.attributes()` API that returns
            # non-parameter / non-buffer attributes in a C++ torch::nn module.
            def generate_attr_equality_checks(module,
                                              script_module_prefix='m_init_by_python',
                                              cpp_module_prefix='m_init_by_cpp'):
                stmts = []
                for name, sub_module in module.named_children():
                    sub_script_module_prefix = '{}.get_module("{}")'.format(script_module_prefix, name)
                    sub_cpp_module_prefix = '{}->{}'.format(cpp_module_prefix, name)
                    stmts = generate_attr_equality_checks(sub_module, sub_script_module_prefix, sub_cpp_module_prefix)
                for name, param in module._parameters.items():
                    stmts.append(CHECK_MODULE_PARAM_EQUALITY.substitute(
                        script_module_prefix=script_module_prefix,
                        cpp_module_prefix=cpp_module_prefix,
                        param_name=name))
                for name, buffer in module._buffers.items():
                    stmts.append(CHECK_MODULE_BUFFER_EQUALITY.substitute(
                        script_module_prefix=script_module_prefix,
                        cpp_module_prefix=cpp_module_prefix,
                        buffer_name=name))
                for name, attr in module.__dict__.items():
                    if name not in TORCH_NN_MODULE_IGNORED_ATTRS:
                        stmts.append(CHECK_MODULE_ATTR_EQUALITY.substitute(
                            script_module_prefix=script_module_prefix,
                            cpp_module_prefix=cpp_module_prefix,
                            attr_name=name))
                return stmts

            device = test_params.device
            python_constructor = test_params.python_constructor
            python_constructor_args = test_params.python_constructor_args
            example_inputs = test_params.example_inputs

            torch.manual_seed(2)
            module = python_constructor(*python_constructor_args).to(device)

            extra_stmts = generate_attr_equality_checks(module)
            assert len(extra_stmts) == test_params.num_attrs_recursive
            extra_stmts_str = ''.join(extra_stmts)
            return (([module], device),
                    generate_test_cpp_sources(
                        test_params=test_params, template=TORCH_NN_MODULE_TEST_INIT, extra_stmts=extra_stmts_str))

        def setup_forward_test(test_params):
            device = test_params.device
            python_constructor = test_params.python_constructor
            python_constructor_args = test_params.python_constructor_args
            example_inputs = test_params.example_inputs

            torch.manual_seed(2)
            module = python_constructor(*python_constructor_args).to(device)
            python_output = module(*example_inputs)

            return (([module], device, python_output, example_inputs),
                    generate_test_cpp_sources(
                        test_params=test_params, template=TORCH_NN_MODULE_TEST_FORWARD, extra_stmts=''))

        def setup_backward_test(test_params):
            device = test_params.device
            python_constructor = test_params.python_constructor
            python_constructor_args = test_params.python_constructor_args
            example_inputs = test_params.example_inputs

            torch.manual_seed(2)
            module = python_constructor(*python_constructor_args).to(device)
            python_output = module(*example_inputs)
            python_output.sum().backward()
            # JIT tracing does not save a module's parameters' gradients into ScriptModule.
            # Instead, we create another module `grad_module` with the same structure as `module`,
            # and use `grad_module`'s parameters to save `module`'s corresponding parameters'
            # gradients. Then, we trace both `module` and `grad_module`, serialize them and
            # pass them into C++ for parity testing.
            grad_module = copy.deepcopy(module)
            for param, grad_param in zip(module.parameters(), grad_module.parameters()):
                if param.grad is not None:
                    grad_param.data = param.grad

            return (([module, grad_module], device, example_inputs),
                    generate_test_cpp_sources(
                        test_params=test_params, template=TORCH_NN_MODULE_TEST_BACKWARD, extra_stmts=''))

        def trace_module(module, example_inputs):
            # JIT tracing does not automatically save a module's non-parameter / non-buffer attributes
            # into a ScriptModule's slots, which means we can't access them via `get_attributes()` in C++.
            # Here, we manually register these attributes into the ScriptModule so that we can access them
            # via `get_attributes()` in C++.
            def register_attrs(module, script_module):
                for sub_module, sub_script_module in zip(module.children(), script_module.children()):
                    register_attrs(sub_module, sub_script_module)
                for key, value in module.__dict__.items():
                    if key not in TORCH_NN_MODULE_IGNORED_ATTRS:
                        script_module._c._register_attribute(
                            key, torch.jit.annotations.ann_to_type(type(value)), value)

            # We use JIT tracing to serialize Python module state, so that we can load it into C++
            traced_script_module = torch.jit.trace(module, example_inputs)
            register_attrs(module, traced_script_module)
            return traced_script_module

        def serialize_module_into_file(script_module):
            module_file = tempfile.NamedTemporaryFile(delete=False)
            script_module.save(module_file.name)
            module_file.close()
            return module_file.name

        def test_methods(test_params):
            module_variant_name = test_params.module_variant_name
            example_inputs = test_params.example_inputs

            args_map = {}

            cpp_sources = TORCH_NN_MODULE_COMMON_TEST_HARNESS + test_params.cpp_sources

            torch_nn_test_methods = [
                ('init', setup_init_test),
                ('forward', setup_forward_test),
                ('backward', setup_backward_test),
            ]
            for method_name, setup_test in torch_nn_test_methods:
                args_map[method_name], test_cpp_sources = setup_test(test_params)
                cpp_sources += test_cpp_sources

            cpp_module = self._compile_cpp_code_inline(
                name=test_params.module_variant_name,
                cpp_sources=cpp_sources,
                functions=['{}_test_{}'.format(
                    test_params.module_variant_name,
                    method_name) for method_name, _ in torch_nn_test_methods])

            for method_name, _ in torch_nn_test_methods:
                args = args_map[method_name]
                modules = args[0]
                script_modules = [trace_module(module, example_inputs) for module in modules]
                module_file_names = [serialize_module_into_file(script_module) for script_module in script_modules]

                cpp_args = module_file_names[:]
                for arg in args[1:]:
                    if isinstance(arg, list):
                        cpp_args += arg
                    else:
                        cpp_args.append(arg)

                try:
                    cpp_test_name = '{}_test_{}'.format(module_variant_name, method_name)
                    cpp_test_fn = getattr(cpp_module, cpp_test_name)
                    if not test_params.has_parity:
                        with self.assertRaisesRegex(RuntimeError, "Parity test failed"):
                            cpp_test_fn(*cpp_args)
                    else:
                        cpp_test_fn(*cpp_args)
                finally:
                    # Ideally we would like to not have to manually delete the file, but NamedTemporaryFile
                    # opens the file, and it cannot be opened multiple times in Windows. To support Windows,
                    # we close the file after creation and try to remove it manually.
                    for module_file_name in module_file_names:
                        try:
                            os.remove(module_file_name)
                        except OSError as e:
                            warnings.warn("Unable to remove {}, got error: {}".format(module_file_name, str(e)))

        test_methods(test_params)


def _compute_module_name(test_params_dict):
    fullname = test_params_dict.get('fullname', None)
    if fullname:
        # NOTE: This doesn't work for some of the `wrap_functional` module tests such as "interpolate_nearest_1d",
        # because in that case the module `interpolate` is not in `torch.nn` but rather in `torch.nn.functional`.
        # We will fix this when we have parity tests for `torch.nn.functional` modules.
        module_name = fullname.split('_')[0]
    else:
        module_name = test_params_dict.get('module_name')
    return module_name


def _process_test_params(test_params_dict, module_metadata, device):
    module_name = _compute_module_name(test_params_dict)
    test_params_dict['constructor'] = test_params_dict.get('constructor', getattr(torch.nn, module_name))
    test = common_nn.TestBase(**test_params_dict)
    module_variant_name = test.get_name()[5:] + (('_' + device) if device != 'cpu' else '')
    example_inputs = test._get_input()

    if type(example_inputs) == tuple:
        example_inputs = list(example_inputs)
    elif type(example_inputs) == torch.Tensor:
        example_inputs = [example_inputs]
    else:
        raise RuntimeError("Unexpected input type: {}".format(type(example_inputs)))

    if device != 'cuda' or TEST_CUDA:
        example_inputs = [x.to(device) for x in example_inputs]
    return TorchNNTestParams(
        module_name=module_name,
        module_variant_name=module_variant_name,
        python_constructor=test.constructor,
        python_constructor_args=test.constructor_args,
        cpp_constructor_args=test_params_dict.get('cpp_constructor_args'),
        example_inputs=example_inputs,
        has_parity=test_params_dict.get('has_parity', True),
        cpp_sources=module_metadata.cpp_sources,
        num_attrs_recursive=module_metadata.num_attrs_recursive,
        device=device,
    )

def has_test(test_name):
    return hasattr(TestCppApiParity, test_name)

def add_test(test_name, test_fn):
    if has_test(test_name):
        raise RuntimeError("Found two tests with the same name: " + test_name)
    setattr(TestCppApiParity, test_name, test_fn)

devices = ['cpu', 'cuda']

torch_nn_test_params_map = {}

all_module_tests = sample_module.module_tests + \
    common_nn.module_tests + \
    common_nn.new_module_tests + \
    common_nn.criterion_tests + \
    common_nn.new_criterion_tests

for test_params_dict in all_module_tests:
    # We skip all `torch.nn.functional` tests for now
    if 'FunctionalModule' in str(test_params_dict.get('constructor', '')):
        continue

    module_name = _compute_module_name(test_params_dict)

    assert hasattr(torch.nn, module_name), \
        "`torch.nn` doesn't have module `{}`. ".format(module_name) + \
        "If you are adding a new test, please set `fullname` using format `ModuleName_desc`, " + \
        "or set `module_name` using format `ModuleName`."

    module_full_name = 'torch.nn.' + module_name
    if module_full_name not in parity_table['torch.nn']:
        raise RuntimeError(
            'Module `{}` is not found in Python / C++ API parity table. Please update parity table at {}.'.format(
                module_full_name, parity_table_path))

    has_impl_parity, _ = parity_table['torch.nn'][module_full_name]

    def add_ctor_args_test_for_module(module_name, has_impl_parity):
        ctor_args_test_name = 'test_torch_nn_{}_ctor_args'.format(module_name)

        def ctor_args_test(self):
            self._test_torch_nn_module_ctor_args(
                module_name=self._testMethodName.replace('test_torch_nn_', '').replace('_ctor_args', ''))

        if not has_impl_parity:
            ctor_args_test = unittest.expectedFailure(ctor_args_test)

        # We only run one constructor args test per module
        if not has_test(ctor_args_test_name):
            add_test(ctor_args_test_name, ctor_args_test)

    def add_variant_test_for_module(module_name, test_params_dict, has_impl_parity):
        module_metadata = torch_nn_modules.module_metadata_map[module_name]
        for device in devices:
            test_params = _process_test_params(
                test_params_dict=test_params_dict,
                module_metadata=module_metadata,
                device=device)
            test_name = 'test_torch_nn_{}'.format(test_params.module_variant_name)
            torch_nn_test_params_map[test_name] = test_params

            def test_fn(self):
                self._test_torch_nn_module_variant(test_params=torch_nn_test_params_map[self._testMethodName])

            if device == 'cuda':
                test_fn = unittest.skipIf(not TEST_CUDA, "CUDA unavailable")(test_fn)

            if not has_impl_parity:
                test_fn = unittest.expectedFailure(test_fn)

            add_test(test_name, test_fn)

    add_ctor_args_test_for_module(module_name, has_impl_parity)
    add_variant_test_for_module(module_name, test_params_dict, has_impl_parity)


# Assert that there exists auto-generated tests for SampleModule.
assert len([name for name in TestCppApiParity.__dict__ if 'SampleModule' in name]) == \
    len(sample_module.module_tests) * len(devices) + 1


if __name__ == "__main__":
    common.run_tests()
