# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2006-present Paulo Henrique Silva <ph.silva@gmail.com>
"""Contract tests against whatever is actually plugged in.

Skipped entirely when no camera is attached, so CI stays green -- but run these
before trusting anything the rest of the suite says, because the rest of the
suite grades the fake and **the fake has been wrong twice**: once about
`POASetImageBin` rescaling the ROI, once about the filter wheel reporting a move
as an error return. Both times the fake agreed with the header, the driver agreed
with the fake, and every test passed.

So the job here is not to re-test the driver. It is to ask the hardware the same
questions the fake answers, and fail if they disagree.

    uv run pytest tests/test_hardware.py -v

Every camera attached is tested, not just the first: an Ares-M PRO is 3008x3008
and divides evenly at every binning, so it cannot exercise the ROI rounding rules
at all. A Sedna-M (3096x2078) can.
"""

import numpy as np
import pytest

from chimera_player_one.sdk.bindings import CameraSdk
from chimera_player_one.sdk.camera import Camera
from chimera_player_one.sdk.enums import POAImgFormat
from chimera_player_one.sdk.simulator import (
    ARES_M_PRO,
    SEDNA_M,
    FakeCameraLibrary,
    FakeCameraSpec,
)


def _attached():
    try:
        return CameraSdk().enumerate()
    except Exception:
        return []


ATTACHED = _attached()
pytestmark = pytest.mark.skipif(not ATTACHED, reason="no Player One camera attached")
MODELS = [p.model for p in ATTACHED]


@pytest.fixture(params=MODELS)
def camera(request):
    with Camera.open(model=request.param) as cam:
        yield cam


class TestEveryAttachedCamera:
    def test_identity_is_readable(self, camera):
        props = camera.properties
        assert props.model and props.serial and props.sensor
        assert props.maxWidth > 0 and props.maxHeight > 0

    @pytest.mark.parametrize("binning", [1, 2, 3, 4])
    def test_geometry_obeys_the_measured_rounding_rules(self, camera, binning):
        """Width down to a multiple of 4, height down to a multiple of 2.

        The height rule is undocumented -- the header states only the width one.
        """
        props = camera.properties
        if binning not in props.binnings:
            pytest.skip(f"{props.model} does not support bin {binning}")
        geometry = camera.configure(
            binning=binning, image_format=POAImgFormat.POA_RAW16
        )
        assert geometry.width % 4 == 0
        assert geometry.height % 2 == 0
        expected_w = props.maxWidth // binning
        expected_h = props.maxHeight // binning
        assert geometry.width == expected_w - (expected_w % 4)
        assert geometry.height == expected_h - (expected_h % 2)

    def test_configure_always_returns_to_full_frame(self, camera):
        """Binning rescales the current ROI rather than resetting it, so
        consecutive configures would shrink the frame if configure() did not ask
        for full frame explicitly. This is the bug the ladder found."""
        props = camera.properties
        camera.configure(binning=3)
        geometry = camera.configure(binning=4)
        expected = props.maxWidth // 4
        assert geometry.width == expected - (expected % 4)

    def test_a_frame_arrives_with_the_promised_shape(self, camera):
        camera.configure(binning=4, image_format=POAImgFormat.POA_RAW16)
        geometry = camera.geometry()
        exposure = camera.expose(0.05)
        assert exposure.data.shape == (geometry.height, geometry.width)
        assert exposure.data.dtype == np.uint16
        assert exposure.started_at is not None

    def test_raw16_is_left_shifted(self, camera):
        """Full scale is (2**bitDepth - 1) << (16 - bitDepth), not 65535.

        Checked without needing light: if the data is shifted, every value is a
        multiple of 1 << (16 - bitDepth).

        **Unbinned only, and that is not a detail.** Binning defaults to
        *averaging* (POA_PIXEL_BIN_SUM is false), and dividing by the bin area
        destroys the low-bit signature -- at bin 4 this camera returns 210, 216,
        207 ... which are not multiples of 4 at all. The first version of this
        test used bin 4 and failed; the conclusion was right and the probe was
        wrong. The saturation *level* is unaffected: an average of values that are
        each at most 65532 is still at most 65532.
        """
        depth = camera.properties.bitDepth
        shift = 16 - depth
        camera.configure(binning=1, image_format=POAImgFormat.POA_RAW16)
        data = camera.expose(0.05).data
        assert np.all(np.asarray(data) % (1 << shift) == 0), (
            f"expected {depth}-bit data left-shifted by {shift}, unbinned"
        )

    def test_binning_averages_and_so_breaks_the_shift_signature(self, camera):
        """Pinned as behaviour, not just as a caveat on the test above.

        It also tells you what binned data *is*: a mean, not a sum, so a binned
        frame does not gain the dynamic range a summed one would.
        """
        depth = camera.properties.bitDepth
        shift = 16 - depth
        if 4 not in camera.properties.binnings:
            pytest.skip("no bin 4 on this camera")
        camera.configure(binning=4, image_format=POAImgFormat.POA_RAW16)
        data = np.asarray(camera.expose(0.05).data)
        assert not np.all(data % (1 << shift) == 0)
        assert data.max() <= ((1 << depth) - 1) << shift


class TestFakeAgreesWithHardware:
    """The fake's claims, put to the hardware.

    Each of these has a counterpart in `test_bindings.py` asserting the same thing
    about `FakeCameraLibrary`. If one of these fails, the fake is lying and every
    green test that depends on it is worthless.
    """

    @staticmethod
    def _spec_for(model):
        for spec in (ARES_M_PRO, SEDNA_M):
            if spec.model == model:
                return spec
        return None

    def test_fake_specs_match_the_real_cameras(self, camera):
        """The fake's bench specs are a claim about these serial numbers."""
        spec = self._spec_for(camera.properties.model)
        if spec is None:
            pytest.skip(f"no fake spec for {camera.properties.model}")
        props = camera.properties
        assert (spec.width, spec.height) == (props.maxWidth, props.maxHeight)
        assert spec.bit_depth == props.bitDepth
        assert spec.pixel_size == pytest.approx(props.pixelSize, abs=0.01)
        assert spec.has_cooler == props.has_cooler
        assert spec.has_st4 == props.has_st4
        assert spec.supports_hardware_bin == props.supports_hardware_bin
        assert list(spec.bins) == props.binnings
        assert list(spec.formats) == props.formats
        assert spec.pid == props.pID

    def test_binning_rescales_the_roi_on_hardware_too(self, camera):
        """The behaviour the fake got wrong, asked directly."""
        sdk, cid = camera._sdk, camera.camera_id
        sdk.set_image_bin(cid, 1)
        sdk.set_image_size(cid, 1024, 1024)
        sdk.set_image_start_pos(cid, 500, 500)
        sdk.set_image_bin(cid, 2)
        assert sdk.get_image_size(cid) == (512, 512), "bin must rescale, not reset"
        assert sdk.get_image_start_pos(cid) == (250, 250), "start must scale too"

    def test_the_same_sequence_gives_the_same_geometry_in_both(self, camera):
        """Drive the fake and the hardware through one sequence and compare.

        This is the test that would have caught both of today's divergences.
        """
        spec = self._spec_for(camera.properties.model)
        if spec is None:
            pytest.skip(f"no fake spec for {camera.properties.model}")

        def sequence(sdk, cid):
            steps = []
            for binning in (1, 3, 4, 2):
                sdk.set_image_bin(cid, binning)
                steps.append(
                    (
                        sdk.get_image_bin(cid),
                        sdk.get_image_size(cid),
                        sdk.get_image_start_pos(cid),
                    )
                )
            return steps

        real = sequence(camera._sdk, camera.camera_id)

        fake_sdk = CameraSdk(FakeCameraLibrary([spec]))
        props = fake_sdk.enumerate()[0]
        fake_sdk.open_camera(props.cameraID)
        fake_sdk.init_camera(props.cameraID)
        try:
            fake = sequence(fake_sdk, props.cameraID)
        finally:
            fake_sdk.close_camera(props.cameraID)

        assert fake == real, (
            "the fake and the hardware disagree about ROI geometry; the fake is "
            f"wrong until proven otherwise.\n  fake: {fake}\n  real: {real}"
        )


def test_a_fake_spec_exists_for_every_attached_camera():
    """A camera on the bench with no fake spec is one the suite cannot cover."""
    known = {ARES_M_PRO.model, SEDNA_M.model}
    unknown = [m for m in MODELS if m not in known]
    assert not unknown, (
        f"attached but not modelled in simulator.py: {unknown}. Add a "
        f"{FakeCameraSpec.__name__} so CI can exercise it."
    )
