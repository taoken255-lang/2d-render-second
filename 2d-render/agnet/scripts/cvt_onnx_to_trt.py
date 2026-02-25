import os
import sys
import torch
from polygraphy.backend.trt import CreateConfig, EngineFromNetwork, NetworkFromOnnxPath, Profile


def onnx_to_trt_for_gridsample(onnx_file, trt_file, fp16=False, plugin_file="./libgrid_sample_3d_plugin.so"):
    import tensorrt as trt

    logger = trt.Logger(trt.Logger.INFO)
    trt.init_libnvinfer_plugins(logger, "")
    plugin_libs = [plugin_file]

    onnx_path = onnx_file
    engine_path = trt_file

    builder = trt.Builder(logger)
    for pluginlib in plugin_libs:
        builder.get_plugin_registry().load_library(pluginlib)
    network = builder.create_network(
        1 << int(trt.NetworkDefinitionCreationFlag.EXPLICIT_BATCH)
    )

    parser = trt.OnnxParser(network, logger)
    res = parser.parse_from_file(onnx_path)  # parse from file
    if not res:
        print(f"Fail parsing {onnx_path}")
        for i in range(parser.num_errors):  # Get error information
            error = parser.get_error(i)
            print(error)  # Print error information
            print(
                f"{error.code() = }\n{error.file() = }\n{error.func() = }\n{error.line() = }\n{error.local_function_stack_size() = }"
            )
            print(
                f"{error.local_function_stack() = }\n{error.node_name() = }\n{error.node_operator() = }\n{error.node() = }"
            )
        parser.clear_errors()
    config = builder.create_builder_config()
    # config.set_memory_pool_limit(trt.MemoryPoolType.WORKSPACE, 1 << 32)
    config.builder_optimization_level = 5
    # Set the flag of hardware compatibility, Hardware-compatible engines are only supported on Ampere and beyond
    cap = torch.cuda.get_device_capability()
    if cap[0] >= 8:
        compatible = True
    else:
        compatible = False

    if compatible:
        config.hardware_compatibility_level = (
            trt.HardwareCompatibilityLevel.AMPERE_PLUS
        )

    if fp16:
        config.set_flag(trt.BuilderFlag.FP16)
        config.set_flag(trt.BuilderFlag.PREFER_PRECISION_CONSTRAINTS)
    config.set_preview_feature(trt.PreviewFeature.PROFILE_SHARING_0806, True)
    exclude_list = [
        "SHAPE",
        "ASSERTION",
        "SHUFFLE",
        "IDENTITY",
        "CONSTANT",
        "CONCATENATION",
        "GATHER",
        "SLICE",
        "CONDITION",
        "CONDITIONAL_INPUT",
        "CONDITIONAL_OUTPUT",
        "FILL",
        "NON_ZERO",
        "ONE_HOT",
    ]
    for i in range(0, network.num_layers):
        layer = network.get_layer(i)
        if str(layer.type)[10:] in exclude_list:
            continue
        if "GridSample" in layer.name:
            print(f"set {layer.name} to float32")
            layer.precision = trt.float32
    config.plugins_to_serialize = plugin_libs
    engineString = builder.build_serialized_network(network, config)
    if engineString is not None:
        with open(engine_path, "wb") as f:
            f.write(engineString)


def convert_onnx_to_trt(onnx_path, trt_path, fp16=False, optimization_level=3, plugin_file=None):
    """
    Convert ONNX model to TensorRT engine using Polygraphy Python API
    """
    try:
        print(f"Converting {onnx_path} to {trt_path}")

        # Load plugin if provided
        if plugin_file and os.path.exists(plugin_file):
            import tensorrt as trt
            logger = trt.Logger(trt.Logger.INFO)
            trt.init_libnvinfer_plugins(logger, "")
            # Load the plugin library
            import ctypes
            ctypes.CDLL(plugin_file)
            print(f"Loaded plugin: {plugin_file}")

        # Create TensorRT config
        config = CreateConfig(
            fp16=fp16,
            builder_optimization_level=optimization_level
        )

        # Build the engine
        engine = EngineFromNetwork(
            NetworkFromOnnxPath(onnx_path),
            config=config
        )

        # Save the engine
        with open(trt_path, 'wb') as f:
            f.write(engine().serialize())

        print(f"Successfully converted to {trt_path}")
        return True

    except Exception as e:
        print(f"Error during conversion: {e}")
        import traceback
        traceback.print_exc()
        return False


def convert_onnx_to_trt_with_profiles(onnx_path, trt_path, fp16=False, optimization_level=3, profiles=None,
                                      plugin_file=None):
    """
    Convert ONNX model to TensorRT engine using Polygraphy Python API with dynamic profiles
    """
    try:
        print(f"Converting {onnx_path} to {trt_path} with dynamic profiles")

        # Load plugin if provided
        if plugin_file and os.path.exists(plugin_file):
            import tensorrt as trt
            logger = trt.Logger(trt.Logger.INFO)
            trt.init_libnvinfer_plugins(logger, "")
            # Load the plugin library
            import ctypes
            ctypes.CDLL(plugin_file)
            print(f"Loaded plugin: {plugin_file}")

        # Create TensorRT config with profiles
        config = CreateConfig(
            fp16=fp16,
            builder_optimization_level=optimization_level,
            profiles=profiles
        )

        # Build the engine
        engine = EngineFromNetwork(
            NetworkFromOnnxPath(onnx_path),
            config=config
        )

        # Save the engine
        with open(trt_path, 'wb') as f:
            f.write(engine().serialize())

        print(f"Successfully converted to {trt_path}")
        return True

    except Exception as e:
        print(f"Error during conversion: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    onnx_path = "./checkpoints/ditto_onnx/"
    trt_path = "./checkpoints/ditto_trt_5070_10.13/"

    # Ensure output directory exists
    os.makedirs(os.path.dirname(trt_path), exist_ok=True)
    grid_sample_plugin_file = os.path.join(onnx_path, "libgrid_sample_3d_plugin.so")

    # Check if plugin file exists
    if not os.path.exists(grid_sample_plugin_file):
        print(f"Warning: Plugin file not found at {grid_sample_plugin_file}")
        print("Please build the plugin first or check the path.")
        grid_sample_plugin_file = None
    names = [i[:-5] for i in os.listdir(onnx_path) if i.endswith(".onnx")]
    for name in names:
        fp16 = False if name in {"motion_extractor", "hubert", "wavlm"} or name.startswith("lmdm") else True
        onnx_file = f"{onnx_path}/{name}.onnx"
        trt_file = f"{trt_path}/{name}_fp{16 if fp16 else 32}.engine"
        if name == "warp_network_ori":
            continue
        if os.path.isfile(trt_file):
            print("=" * 20, f"{name} skip", "=" * 20)
            continue
        if name == "hubert":
            # Create profiles for hubert with dynamic shapes
            profiles = [
                Profile().add("input_values", min=(1, 3240), opt=(1, 6480), max=(1, 12960)),
            ]
            success = convert_onnx_to_trt_with_profiles(onnx_file, trt_file, fp16=fp16, optimization_level=5,
                                                        profiles=profiles)
            if not success:
                print(f"Failed to convert {name}")
                continue
        elif name == "warp_network":
            onnx_to_trt_for_gridsample(onnx_file, trt_file, fp16, plugin_file=grid_sample_plugin_file)
        else:
            # else:
            # Use the general conversion function
            success = convert_onnx_to_trt(onnx_file, trt_file, fp16=fp16, optimization_level=5)
            if not success:
                print(f"Failed to convert {name}")
                continue


    print("Conversion completed successfully!")

# RUN python3 agnet/scripts/cvt_onnx_to_trt.py FROM /app