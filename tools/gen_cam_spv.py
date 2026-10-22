#!/usr/bin/env python3
"""Generate brightness and optional reverse-key variants of the camera shaders.

tracked_camera_reprojection_simplified[_sharpen]_rgb_nv12_ps draw the camera.  They
output (camera.rgb * tint.rgb * opacity, opacity).  Measured with the layer's camera
probe while streaming:
  - keyed view (scene app + dashboard/desktop up, camera blended with
    ONE_MINUS_DST_ALPHA):  tint 0.7, opacity 0.6
  - "room view for scene apps" (pass-only, camera drawn opaque): tint 0.7, opacity 1.0
So keyed passthrough is 40% darker than pass-only.  We scale only the opacity that
multiplies the colour: rgb * min(opacity * gain, 1).  Gain 1/0.6 lifts the keyed view
to pass-only brightness, leaves pass-only (already 1.0) unchanged, and fades to 0
still reach 0.  The output alpha is untouched.

Optional <brightness> scales the tint for both views: tint.rgb -> min(tint * b, 4).
1.4286 undoes the compositor's 0.7 tint.

usage: gen_cam_spv.py <orig.spvasm> <out.spv> <gain> [brightness]
       gen_cam_spv.py <orig.spvasm> <out.spv> <gain> <brightness> reverse R G B threshold softness spill

Reverse mode keys the camera colour before opacity. Its RGB is already tint-adjusted,
so comparison uses normalized chroma to avoid the compositor's variable brightness.
The result is premultiplied by the camera visibility mask. The Vulkan layer changes
the camera pipeline blend to ONE / ONE_MINUS_SRC_ALPHA for this variant.
The pass-only camera view uses opacity 1; its key mask is forced to 1 so Room View
remains opaque. The streamed scene blend was measured at opacity 0.6.
"""
import math
from pathlib import Path
import re
import subprocess
import sys

# the opacity (ubo member 9 .w) load that feeds  rgb * opacity
OPACITY_RE = re.compile(r"(%\w+) = OpAccessChain %_ptr_Uniform_float %\w+ %int_9 %uint_3\n"
                        r"(\s*)(%\w+) = OpLoad %float \1\n"
                        r"(\s*%\w+ = OpVectorTimesScalar %v3float (%\w+) )\3\n")
# tint.rgb = shuffle(load(ubo member 9))
TINT_RE = re.compile(r"(%\w+) = OpAccessChain %_ptr_Uniform_v4float %\w+ %int_9\n"
                     r"(\s*)(%\w+) = OpLoad %v4float \1\n"
                     r"\s*(%\w+) = OpVectorShuffle %v3float \3 \3 0 1 2\n")
FUNC = "%4 = OpFunction %void None %3"
OUTPUT_RE = re.compile(r"^(%\w+) = OpVariable %_ptr_Output_v4float Output$", re.M)


def only(regex, asm, what):
    m = list(regex.finditer(asm))
    assert len(m) == 1, "unexpected shader layout (SteamVR update?): " + what
    return m[0]


def main():
    src, out, gain = sys.argv[1], sys.argv[2], float(sys.argv[3])
    bright = float(sys.argv[4]) if len(sys.argv) > 4 else 1.0
    reverse = len(sys.argv) > 5
    if reverse:
        assert len(sys.argv) == 12 and sys.argv[5] == "reverse", "expected reverse R G B threshold softness spill"
        red, green, blue, threshold, softness, spill = map(float, sys.argv[6:])
        assert all(math.isfinite(x) for x in (red, green, blue, threshold, softness, spill))
        assert 0 <= min(red, green, blue) and max(red, green, blue) <= 1
        assert threshold >= 0 and softness > 0 and 0 <= spill <= 1
    asm = Path(src).read_text()
    for marker in (FUNC, "%1 = OpExtInstImport \"GLSL.std.450\"", " 9 Offset 304\n"):
        assert asm.count(marker) == 1, "unexpected shader layout (SteamVR update?): " + marker
    consts = [
        "%mk_gain = OpConstant %float " + repr(gain),
        "%mk_one = OpConstant %float 1",
    ]
    m = only(OPACITY_RE, asm, "opacity load")
    ind = m.group(2)
    camera_rgb = m.group(5)
    opacity = m.group(3)
    repl = (m.group(0)[:m.start(4) - m.start(0)]
            + ind + "%mk_og = OpFMul %float " + m.group(3) + " %mk_gain\n"
            + ind + "%mk_op = OpExtInst %float %1 FMin %mk_og %mk_one\n"
            + m.group(4) + "%mk_op\n")
    asm = asm[:m.start()] + repl + asm[m.end():]
    if bright != 1.0:
        t = only(TINT_RE, asm, "tint load")
        ind = t.group(2)
        repl = (t.group(0)[:t.start(4) - t.start(0)]
                + "%mk_tint = OpVectorShuffle %v3float {0} {0} 0 1 2\n".format(t.group(3))
                + ind + "%mk_tb = OpVectorTimesScalar %v3float %mk_tint %mk_bright\n"
                + ind + t.group(4) + " = OpExtInst %v3float %1 FMin %mk_tb %mk_max3\n")
        asm = asm[:t.start()] + repl + asm[t.end():]
        consts += ["%mk_bright = OpConstant %float " + repr(bright), "%mk_max = OpConstant %float 4",
                   "%mk_max3 = OpConstantComposite %v3float %mk_max %mk_max %mk_max"]
    if reverse:
        # Locate the single fragment colour output; refuse to patch a changed shader layout.
        output_var = only(OUTPUT_RE, asm, "fragment output").group(1)
        store = only(re.compile(r"^([ \t]*)OpStore " + re.escape(output_var) + r" (%\w+)$", re.M),
                     asm, "fragment output store")
        norm = math.sqrt(red * red + green * green + blue * blue)
        assert norm > 0, "reverse key colour cannot be black"
        red, green, blue = red / norm, green / norm, blue / norm
        y = 0.2126 * red + 0.7152 * green + 0.0722 * blue
        kb, kr = (blue - y) / 1.8556, (red - y) / 1.5748
        consts += [
            "%mk_eps = OpConstant %float 0.0001",
            "%mk_f0 = OpConstant %float 0",
            "%mk_scene_opacity = OpConstant %float 0.99",
            "%mk_lr = OpConstant %float 0.2126", "%mk_lg = OpConstant %float 0.7152",
            "%mk_lb = OpConstant %float 0.0722",
            "%mk_luma = OpConstantComposite %v3float %mk_lr %mk_lg %mk_lb",
            "%mk_icb = OpConstant %float " + repr(1 / 1.8556),
            "%mk_icr = OpConstant %float " + repr(1 / 1.5748),
            "%mk_kb = OpConstant %float " + repr(kb), "%mk_kr = OpConstant %float " + repr(kr),
            "%mk_key = OpConstantComposite %v2float %mk_kb %mk_kr",
            "%mk_t0 = OpConstant %float " + repr(threshold),
            "%mk_t1 = OpConstant %float " + repr(threshold + softness),
            "%mk_t3 = OpConstant %float " + repr(threshold + 3 * softness),
            "%mk_sp = OpConstant %float " + repr(spill),
        ]
        value = store.group(2)
        code = [
            "%mk_len = OpExtInst %float %1 Length " + camera_rgb,
            "%mk_safe = OpExtInst %float %1 FMax %mk_len %mk_eps",
            "%mk_inv = OpFDiv %float %mk_one %mk_safe",
            "%mk_cam = OpVectorTimesScalar %v3float " + camera_rgb + " %mk_inv",
            "%mk_r = OpCompositeExtract %float %mk_cam 0",
            "%mk_b = OpCompositeExtract %float %mk_cam 2",
            "%mk_y = OpDot %float %mk_cam %mk_luma",
            "%mk_cb0 = OpFSub %float %mk_b %mk_y",
            "%mk_cb = OpFMul %float %mk_cb0 %mk_icb",
            "%mk_cr0 = OpFSub %float %mk_r %mk_y",
            "%mk_cr = OpFMul %float %mk_cr0 %mk_icr",
            "%mk_cbcr = OpCompositeConstruct %v2float %mk_cb %mk_cr",
            "%mk_d = OpExtInst %float %1 Distance %mk_cbcr %mk_key",
            "%mk_mask = OpExtInst %float %1 SmoothStep %mk_t0 %mk_t1 %mk_d",
            "%mk_stream = OpFOrdLessThan %bool " + opacity + " %mk_scene_opacity",
            "%mk_visible = OpSelect %float %mk_stream %mk_mask %mk_one",
            "%mk_ws = OpExtInst %float %1 SmoothStep %mk_t0 %mk_t3 %mk_d",
            "%mk_wi = OpFSub %float %mk_one %mk_ws",
            "%mk_wraw = OpFMul %float %mk_wi %mk_sp",
            "%mk_w = OpSelect %float %mk_stream %mk_wraw %mk_f0",
            "%mk_raw = OpVectorShuffle %v3float " + value + " " + value + " 0 1 2",
            "%mk_or = OpCompositeExtract %float %mk_raw 0",
            "%mk_ogreen = OpCompositeExtract %float %mk_raw 1",
            "%mk_ob = OpCompositeExtract %float %mk_raw 2",
            "%mk_rb = OpExtInst %float %1 FMax %mk_or %mk_ob",
            "%mk_gc = OpExtInst %float %1 FMin %mk_ogreen %mk_rb",
            "%mk_g2 = OpExtInst %float %1 FMix %mk_ogreen %mk_gc %mk_w",
            "%mk_clean = OpCompositeConstruct %v3float %mk_or %mk_g2 %mk_ob",
            "%mk_rgb = OpVectorTimesScalar %v3float %mk_clean %mk_visible",
            "%mk_alpha = OpFMul %float " + opacity + " %mk_visible",
            "%mk_o0 = OpCompositeExtract %float %mk_rgb 0",
            "%mk_o1 = OpCompositeExtract %float %mk_rgb 1",
            "%mk_o2 = OpCompositeExtract %float %mk_rgb 2",
            "%mk_out = OpCompositeConstruct %v4float %mk_o0 %mk_o1 %mk_o2 %mk_alpha",
            "OpStore " + output_var + " %mk_out",
        ]
        asm = asm[:store.start()] + store.group(1) + ("\n" + store.group(1)).join(code) + asm[store.end():]
    asm = asm.replace(FUNC, "\n".join(consts) + "\n" + FUNC)
    tmp = out + ".spvasm"
    Path(tmp).write_text(asm)
    subprocess.run(["spirv-as", "--target-env", "vulkan1.1", tmp, "-o", out], check=True)
    subprocess.run(["spirv-val", "--target-env", "vulkan1.1", out], check=True)
    print("wrote", out)


if __name__ == "__main__":
    main()
