import importlib.util
from pathlib import Path


def load_adapter():
    path = Path(__file__).parents[2] / "scripts" / "seedvr2-adapter.py"
    spec = importlib.util.spec_from_file_location("seedvr2_adapter", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_3b_safe_contract_uses_bounded_2x_seedvr2_parameters(tmp_path):
    """Changing shipped 3B profile parameters must make this test fail."""
    adapter = load_adapter()
    command = adapter.build_seedvr2_command(
        input_path=tmp_path / "input.mp4",
        output_path=tmp_path / "video-only.mp4",
        model_dir=tmp_path / "models",
        model_name="three-b.safetensors",
        preset="3b-safe",
        color_correction="lab",
        source_width=1280,
        source_height=720,
        python="seed-python",
        official_cli=tmp_path / "inference_cli.py",
    )

    assert command[:2] == ["seed-python", str(tmp_path / "inference_cli.py")]
    assert command[command.index("--resolution") + 1] == "1440"
    assert command[command.index("--batch_size") + 1] == "5"
    assert command[command.index("--chunk_size") + 1] == "25"
    assert command[command.index("--temporal_overlap") + 1] == "4"
    assert "--vae_encode_tiled" in command
    assert "--vae_decode_tiled" in command
    assert "--10bit" in command


def test_final_mp4_contract_is_hevc_main10_and_transcodes_audio(tmp_path):
    """Dropping HEVC Main10 or AAC audio output guarantees must make this test fail."""
    adapter = load_adapter()
    command = adapter.final_mp4_command(
        video=tmp_path / "video-only.mp4",
        source=tmp_path / "source.mov",
        output=tmp_path / "output.mp4",
        ffmpeg="ffmpeg",
    )

    assert command[command.index("-c:v") + 1] == "libx265"
    assert command[command.index("-pix_fmt") + 1] == "yuv420p10le"
    assert command[command.index("-tag:v") + 1] == "hvc1"
    assert command[command.index("-c:a") + 1] == "aac"
    input_indexes = [index for index, item in enumerate(command) if item == "-i"]
    assert len(input_indexes) == 2
    for input_index in input_indexes:
        assert command[input_index - 2 : input_index] == ["-protocol_whitelist", "file"]


def test_adapter_public_parser_rejects_unimplemented_realesrgan_profile():
    """Advertising unavailable Real-ESRGAN from shipped adapter must make this test fail."""
    adapter = load_adapter()

    try:
        adapter.parser().parse_args(
            [
                "--input", "input.mp4", "--output", "output.mp4", "--preset", "realesrgan-conservative",
                "--color-correction", "lab", "--mode", "full", "--model-dir", "models",
            ]
        )
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError("unimplemented Real-ESRGAN profile was accepted")


def test_adapter_rejects_target_resolution_above_safety_limit(tmp_path):
    """Forwarding an extreme source resolution into SeedVR2 must make this test fail."""
    adapter = load_adapter()

    try:
        adapter.build_seedvr2_command(
            input_path=tmp_path / "input.mp4",
            output_path=tmp_path / "output.mp4",
            model_dir=tmp_path / "models",
            model_name="three-b.safetensors",
            preset="3b-safe",
            color_correction="lab",
            source_width=8192,
            source_height=4320,
            python="python",
            official_cli=tmp_path / "cli.py",
        )
    except ValueError as error:
        assert str(error) == "Target resolution exceeds safety limit"
    else:
        raise AssertionError("extreme target resolution was accepted")
