"""Check camera shader patch anchors without SteamVR's proprietary shader files."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "gen_cam_spv.py"
spec = importlib.util.spec_from_file_location("gen_cam_spv", SCRIPT)
gen = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gen)

CAMERA_ASM = '''%1 = OpExtInstImport "GLSL.std.450"
OpMemberDecorate %ubo_type 9 Offset 304
%out = OpVariable %_ptr_Output_v4float Output
%4 = OpFunction %void None %3
%tc = OpAccessChain %_ptr_Uniform_v4float %ubo %int_9
%tv = OpLoad %v4float %tc
%tint = OpVectorShuffle %v3float %tv %tv 0 1 2
%ac = OpAccessChain %_ptr_Uniform_float %ubo %int_9 %uint_3
%op = OpLoad %float %ac
%mul = OpVectorTimesScalar %v3float %camera_tinted %op
OpStore %out %result
OpFunctionEnd
'''


class CameraGeneratorTest(unittest.TestCase):
    def generate(self, args, asm=CAMERA_ASM):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "cam.spvasm"
            output = Path(tmp) / "cam.spv"
            source.write_text(asm)
            with patch.object(sys, "argv", [str(SCRIPT), str(source), str(output), *args]), \
                 patch.object(gen.subprocess, "run"), patch("builtins.print"):
                gen.main()
            return Path(str(output) + ".spvasm").read_text()

    def test_brightness_variant_keeps_original_output(self):
        asm = self.generate(["1.6667", "1.2"])
        self.assertIn("OpStore %out %result", asm)
        self.assertNotIn("%mk_mask", asm)

    def test_reverse_variant_masks_camera_rgb_and_alpha(self):
        asm = self.generate(["1.6667", "1.2", "reverse", "0", "1", "0", "0.18", "0.1", "0.5"])
        self.assertIn("%mk_len = OpExtInst %float %1 Length %camera_tinted", asm)
        self.assertIn("%mk_alpha = OpFMul %float %op %mk_visible", asm)
        self.assertIn("%mk_rgb = OpVectorTimesScalar %v3float %mk_clean %mk_visible", asm)
        self.assertIn("%mk_visible = OpSelect %float %mk_stream %mk_mask %mk_one", asm)
        self.assertIn("OpStore %out %mk_out", asm)
        self.assertNotIn("OpStore %out %result", asm)

    def test_unknown_output_layout_is_rejected(self):
        with self.assertRaisesRegex(AssertionError, "unexpected shader layout"):
            self.generate(["1", "1", "reverse", "0", "1", "0", "0.18", "0.1", "0.5"],
                          CAMERA_ASM.replace("%_ptr_Output_v4float", "%unexpected_output"))


if __name__ == "__main__":
    unittest.main()
