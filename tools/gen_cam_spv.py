#!/usr/bin/env python3
"""Generate a brightness-corrected variant of vrcompositor's passthrough camera shaders.

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
"""
import re
import subprocess
import sys

# the opacity (ubo member 9 .w) load that feeds  rgb * opacity
OPACITY_RE = re.compile(r"(%\w+) = OpAccessChain %_ptr_Uniform_float %\w+ %int_9 %uint_3\n"
                        r"(\s*)(%\w+) = OpLoad %float \1\n"
                        r"(\s*%\w+ = OpVectorTimesScalar %v3float %\w+ )\3\n")
# tint.rgb = shuffle(load(ubo member 9))
TINT_RE = re.compile(r"(%\w+) = OpAccessChain %_ptr_Uniform_v4float %\w+ %int_9\n"
                     r"(\s*)(%\w+) = OpLoad %v4float \1\n"
                     r"\s*(%\w+) = OpVectorShuffle %v3float \3 \3 0 1 2\n")
FUNC = "%4 = OpFunction %void None %3"


def only(regex, asm, what):
    m = list(regex.finditer(asm))
    assert len(m) == 1, "unexpected shader layout (SteamVR update?): " + what
    return m[0]


def main():
    src, out, gain = sys.argv[1], sys.argv[2], float(sys.argv[3])
    bright = float(sys.argv[4]) if len(sys.argv) > 4 else 1.0
    asm = open(src).read()
    for marker in (FUNC, "%1 = OpExtInstImport \"GLSL.std.450\"", " 9 Offset 304\n"):
        assert asm.count(marker) == 1, "unexpected shader layout (SteamVR update?): " + marker
    consts = [
        "%mk_gain = OpConstant %float " + repr(gain),
        "%mk_one = OpConstant %float 1",
    ]
    m = only(OPACITY_RE, asm, "opacity load")
    ind = m.group(2)
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
    asm = asm.replace(FUNC, "\n".join(consts) + "\n" + FUNC)
    tmp = out + ".spvasm"
    open(tmp, "w").write(asm)
    subprocess.run(["spirv-as", "--target-env", "vulkan1.1", tmp, "-o", out], check=True)
    subprocess.run(["spirv-val", "--target-env", "vulkan1.1", out], check=True)
    print("wrote", out)


if __name__ == "__main__":
    main()
