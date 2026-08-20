# SPDX-License-Identifier: GPL-2.0-or-later
# SPDX-FileCopyrightText: 2006-present Paulo Henrique Silva <ph.silva@gmail.com>
"""The marshalling layer, driven against the fake library.

Every test here runs the *real* ``bindings.py`` -- real ctypes structs, real
pointer out-parameters, real union packing, real error mapping. Only the shared
object is fake. That is the whole reason the seam is at the CDLL boundary.
"""

import ctypes

import numpy as np
import pytest

from chimera_player_one.sdk.bindings import CameraSdk, FilterWheelSdk
from chimera_player_one.sdk.enums import (
    POACameraState,
    POAConfig,
    POAErrors,
    POAImgFormat,
    POAValueType,
    PWErrors,
    PWState,
)
from chimera_player_one.sdk.errors import POAError, PWError
from chimera_player_one.sdk.simulator import (
    ARES_M_PRO,
    SEDNA_M,
    FakeCameraLibrary,
    FakeFilterWheelLibrary,
)
from chimera_player_one.sdk.structs import (
    POACameraProperties,
    POAConfigAttributes,
    POAConfigValue,
)


@pytest.fixture
def lib():
    return FakeCameraLibrary([ARES_M_PRO, SEDNA_M])


@pytest.fixture
def sdk(lib):
    return CameraSdk(lib)


@pytest.fixture
def camera(sdk):
    """An opened, initialised Ares-M PRO."""
    props = sdk.enumerate()[0]
    sdk.open_camera(props.cameraID)
    sdk.init_camera(props.cameraID)
    yield props.cameraID
    sdk.close_camera(props.cameraID)


class TestStructLayout:
    def test_sizes_match_the_c_headers(self):
        """A struct that is silently the wrong size reads plausible garbage --
        every field after the mistake is offset, and nothing errors. 992 is the
        value measured against the real 3.10.1 library."""
        assert ctypes.sizeof(POACameraProperties) == 992
        assert ctypes.sizeof(POAConfigValue) == 8
        assert ctypes.sizeof(POAConfigAttributes) == 304

    def test_config_value_union_overlaps(self):
        """It must be a union, not a struct: the members share storage."""
        value = POAConfigValue()
        value.floatValue = 1.5
        assert value.intValue != 0, "writing the float must be visible as an int"

    def test_poa_exp_is_the_last_member(self):
        """POA_EXP was appended at 31 in SDK 3.8.0 specifically to keep the ABI.
        If it ever moves, every config id after it shifts and settings go to the
        wrong knob silently."""
        assert POAConfig.POA_EXP == 31
        assert POAConfig.POA_EXPOSURE == 0


class TestEnumeration:
    def test_enumerate_reads_both_cameras(self, sdk):
        cameras = sdk.enumerate()
        assert [c.model for c in cameras] == ["Ares-M PRO", "Sedna-M"]
        assert cameras[0].sensor == "IMX533"
        assert cameras[0].maxWidth == 3008
        assert cameras[1].pixelSize == pytest.approx(2.40)

    def test_properties_before_scan_are_refused(self, lib, sdk):
        """POAGetCameraCount is the bus scan, not an accessor. Asking for
        properties first returns INVALID_INDEX and a zeroed struct, which reads
        as a broken camera rather than a call-order bug. Measured on hardware."""
        assert lib.scanned is False
        with pytest.raises(POAError) as excinfo:
            sdk.get_camera_properties(0)
        assert excinfo.value.error is POAErrors.POA_ERROR_INVALID_INDEX

    def test_enumerate_scans_first(self, sdk):
        """So no caller has to know the rule above."""
        assert sdk.enumerate()[0].model == "Ares-M PRO"

    def test_sentinel_arrays_are_truncated_not_filtered(self, sdk):
        """The padding past the terminator is zero, and zero is POA_RAW8. A mono
        camera that reports seven formats has been decoded by filtering."""
        props = sdk.enumerate()[0]
        assert list(props.imgFormats) == [0, 1, -1, 0, 0, 0, 0, 0]
        assert props.formats == [POAImgFormat.POA_RAW8, POAImgFormat.POA_RAW16]
        assert props.binnings == [1, 2, 3, 4]


class TestConfigUnion:
    def test_float_config_round_trips_as_float(self, sdk, camera):
        """POA_EXP is VAL_FLOAT. Packed into the int member it would read back as
        a denormal, and nothing would raise."""
        sdk.set_config(camera, POAConfig.POA_EXP, 2.5)
        value, is_auto = sdk.get_config(camera, POAConfig.POA_EXP)
        assert value == pytest.approx(2.5)
        assert is_auto is False

    def test_int_config_round_trips_as_int(self, sdk, camera):
        sdk.set_config(camera, POAConfig.POA_GAIN, 220)
        value, _ = sdk.get_config(camera, POAConfig.POA_GAIN)
        assert value == 220
        assert isinstance(value, int)

    def test_bool_config_round_trips_as_bool(self, sdk, camera):
        sdk.set_config(camera, POAConfig.POA_COOLER, True)
        value, _ = sdk.get_config(camera, POAConfig.POA_COOLER)
        assert value is True

    def test_value_types_are_cached_not_re_queried(self, sdk, lib, camera):
        sdk.get_config_value_type(POAConfig.POA_EXP)
        before = lib.calls.count("POAGetConfigValueType")
        for _ in range(5):
            sdk.get_config_value_type(POAConfig.POA_EXP)
        assert lib.calls.count("POAGetConfigValueType") == before

    def test_attributes_report_the_float_range(self, sdk, camera):
        attrs = sdk.get_config_attributes_by_id(camera, POAConfig.POA_EXP)
        assert attrs.valueType == POAValueType.VAL_FLOAT
        assert attrs.maxValue.floatValue == pytest.approx(7200.0)
        assert attrs.name == "POA_EXP"


class TestGeometry:
    def test_binning_rewrites_size_and_start_position(self, sdk, camera):
        """The header says so, and a driver that does not re-read both allocates
        a buffer of the wrong shape.

        The start position **scales** rather than resetting -- this test asserted
        (0, 0) until hardware said (50, 50). See TestBinningRescalesTheRoi."""
        sdk.set_image_start_pos(camera, 100, 100)
        sdk.set_image_bin(camera, 2)
        assert sdk.get_image_bin(camera) == 2
        assert sdk.get_image_size(camera) == (1504, 1504)
        assert sdk.get_image_start_pos(camera) == (50, 50)

    def test_width_is_rounded_to_a_multiple_of_four(self, sdk, camera):
        """POASetImageSize adjusts silently; the caller must read it back."""
        sdk.set_image_size(camera, 1023, 1000)
        assert sdk.get_image_size(camera) == (1020, 1000)

    def test_geometry_changes_are_refused_while_exposing(self, sdk, camera):
        sdk.start_exposure(camera, single_frame=True)
        with pytest.raises(POAError) as excinfo:
            sdk.set_image_bin(camera, 2)
        assert excinfo.value.error is POAErrors.POA_ERROR_EXPOSING
        sdk.stop_exposure(camera)

    def test_unsupported_format_is_refused(self, sdk, camera):
        with pytest.raises(POAError):
            sdk.set_image_format(camera, POAImgFormat.POA_RGB24)


class TestExposure:
    def _grab(self, sdk, camera, fmt=POAImgFormat.POA_RAW16):
        sdk.set_image_format(camera, fmt)
        sdk.set_config(camera, POAConfig.POA_EXP, 0.001)
        width, height = sdk.get_image_size(camera)
        buffer = np.zeros(width * height * fmt.bytes_per_pixel, dtype=np.uint8)
        sdk.start_exposure(camera, single_frame=True)
        for _ in range(1000):
            if sdk.image_ready(camera):
                break
        sdk.get_image_data(camera, buffer, timeout_ms=500)
        return buffer, width, height

    def test_snap_produces_a_frame_of_the_right_shape(self, sdk, camera):
        sdk.set_image_bin(camera, 4)
        buffer, width, height = self._grab(sdk, camera)
        frame = buffer.view("<u2").reshape(height, width)
        assert frame.shape == (752, 752)
        assert frame.max() > frame.min(), (
            "the fake should produce structure, not a flat field"
        )

    def test_state_reports_exposing(self, sdk, camera):
        assert sdk.get_camera_state(camera) is POACameraState.STATE_OPENED
        sdk.start_exposure(camera, single_frame=True)
        assert sdk.get_camera_state(camera) is POACameraState.STATE_EXPOSING
        sdk.stop_exposure(camera)

    def test_snap_rearms_without_stop(self, sdk, camera):
        """Measured behaviour of the SDK: in snap mode you re-arm by calling
        StartExposure again, with no StopExposure in between."""
        sdk.set_image_bin(camera, 4)
        first, _, _ = self._grab(sdk, camera)
        second, _, _ = self._grab(sdk, camera)
        assert not np.array_equal(first, second), "consecutive frames should differ"

    def test_buffer_must_be_uint8(self, sdk, camera):
        """The size argument is nbytes; a uint16 array passed with the vendor's
        `.size` spelling under-reports by half."""
        sdk.start_exposure(camera, single_frame=True)
        with pytest.raises(TypeError, match="uint8"):
            sdk.get_image_data(camera, np.zeros(16, dtype=np.uint16), 100)

    def test_undersized_buffer_is_refused_by_the_sdk(self, sdk, camera):
        sdk.set_image_format(camera, POAImgFormat.POA_RAW16)
        sdk.start_exposure(camera, single_frame=True)
        with pytest.raises(POAError) as excinfo:
            sdk.get_image_data(camera, np.zeros(16, dtype=np.uint8), 100)
        assert excinfo.value.error is POAErrors.POA_ERROR_SIZE_LESS


class TestFaultInjection:
    def test_timeout_mid_exposure_raises(self, sdk, lib, camera):
        lib.fail_once["POAGetImageData"] = POAErrors.POA_ERROR_TIMEOUT
        sdk.start_exposure(camera, single_frame=True)
        with pytest.raises(POAError) as excinfo:
            sdk.get_image_data(camera, np.zeros(4, dtype=np.uint8), 10)
        assert excinfo.value.error is POAErrors.POA_ERROR_TIMEOUT

    def test_disconnect_between_frames(self, sdk, lib, camera):
        """A USB cable coming loose is not a hypothetical, and it happens between
        frames far more often than during one."""
        lib.disconnect_after_frames = 0
        with pytest.raises(POAError) as excinfo:
            sdk.start_exposure(camera, single_frame=True)
        assert excinfo.value.error is POAErrors.POA_ERROR_DEVICE_NOT_FOUND

    def test_cooler_that_never_reaches_setpoint(self, sdk, lib, camera):
        lib.cooler_reaches_setpoint = False
        sdk.set_config(camera, POAConfig.POA_TARGET_TEMP, -10)
        sdk.set_config(camera, POAConfig.POA_COOLER, True)
        temperature, _ = sdk.get_config(camera, POAConfig.POA_TEMPERATURE)
        assert temperature > -10, "the point of this fixture is that it never arrives"

    def test_access_denied_is_recognisable(self, sdk, lib):
        """The Linux 'you forgot the udev rules' failure needs a named test so
        the driver can explain it instead of printing a code."""
        lib.fail_always["POAOpenCamera"] = POAErrors.POA_ERROR_ACCESS_DENIED
        with pytest.raises(POAError) as excinfo:
            sdk.open_camera(0)
        assert excinfo.value.is_access_denied

    def test_unknown_error_code_keeps_the_number(self, sdk, lib):
        """An SDK newer than this binding must not crash the binding."""
        lib.fail_always["POAOpenCamera"] = 9999
        with pytest.raises(POAError) as excinfo:
            sdk.open_camera(0)
        assert excinfo.value.code == 9999
        assert excinfo.value.error is None


class TestSignatureBinding:
    def test_argtypes_are_bound_once_not_per_call(self, lib):
        """The vendor wrapper reassigns argtypes inside each call, alternating an
        int and a double signature on POASetConfig. Two chimera threads can
        interleave there and marshal a float as an int."""
        CameraSdk(lib)
        before = lib.POASetConfig.argtypes
        CameraSdk(lib)
        assert lib.POASetConfig.argtypes is before

    def test_presets_come_from_the_camera(self, sdk, camera):
        presets = sdk.get_gains_and_offsets(camera)
        assert presets["unity_gain"] == 130
        assert presets["offset_highest_dr"] == 35


class TestFilterWheel:
    @pytest.fixture
    def wheel(self):
        lib = FakeFilterWheelLibrary(move_time=0.0)
        sdk = FilterWheelSdk(lib)
        props = sdk.enumerate()[0]
        sdk.open(props.Handle)
        return sdk, props

    def test_enumerate(self, wheel):
        sdk, props = wheel
        assert props.name == "POA Phoenix Wheel"
        assert props.PositionCount == 7

    def test_goto_and_read_back(self, wheel):
        sdk, props = wheel
        sdk.goto_position(props.Handle, 3)
        assert sdk.get_position(props.Handle) == 3
        assert sdk.get_state(props.Handle) is PWState.PW_STATE_OPENED

    def test_positions_are_zero_based(self, wheel):
        """Matching chimera's filter list index directly, with no +1 anywhere."""
        sdk, props = wheel
        sdk.goto_position(props.Handle, 0)
        assert sdk.get_position(props.Handle) == 0
        with pytest.raises(PWError) as excinfo:
            sdk.goto_position(props.Handle, props.PositionCount)
        assert excinfo.value.error is PWErrors.PW_ERROR_INVALID_ARGU

    def test_firmware_stores_aliases_and_offsets(self, wheel):
        sdk, props = wheel
        assert sdk.get_filter_alias(props.Handle, 0) == "U"
        assert sdk.get_focus_offset(props.Handle, 4) == 25

    def test_moving_wheel_reports_minus_one(self):
        """MEASURED: the SDK signals a move by RETURNING PW_ERROR_IS_MOVING from
        POAGetCurrentPosition, not by answering -1.

        Both obvious readings are wrong -- the wire protocol uses 0xFF and the
        header describes -1 -- so a caller that only checks the value gets an
        exception at the one moment it is most likely to be polling. The binding
        normalises it to -1 in exactly one place; this pins that it does not
        propagate as an error."""
        lib = FakeFilterWheelLibrary(move_time=1000.0)
        sdk = FilterWheelSdk(lib)
        handle = sdk.enumerate()[0].Handle
        sdk.open(handle)
        sdk.goto_position(handle, 5)
        assert sdk.get_position(handle) == -1
        assert sdk.get_state(handle) is PWState.PW_STATE_MOVING

    def test_other_position_errors_still_raise(self):
        """Normalising IS_MOVING must not swallow every other failure."""
        lib = FakeFilterWheelLibrary(move_time=0.0)
        sdk = FilterWheelSdk(lib)
        handle = sdk.enumerate()[0].Handle
        sdk.open(handle)
        lib.fail_once["POAGetCurrentPosition"] = PWErrors.PW_ERROR_NOT_OPENED
        with pytest.raises(PWError) as excinfo:
            sdk.get_position(handle)
        assert excinfo.value.error is PWErrors.PW_ERROR_NOT_OPENED


class TestBinningRescalesTheRoi:
    """Measured on an Ares-M PRO, 2026-08-20, and it is not what the header reads like.

    `POASetImageBin` **rescales the current ROI** rather than resetting to full
    frame, and the rounding loss compounds across successive changes. The fake
    reproduced the intuitive behaviour until hardware said otherwise -- which is
    exactly how a fake becomes a liability, so these numbers are pinned.
    """

    @pytest.fixture
    def camera(self):
        sdk = CameraSdk(FakeCameraLibrary([ARES_M_PRO]))
        props = sdk.enumerate()[0]
        sdk.open_camera(props.cameraID)
        sdk.init_camera(props.cameraID)
        return sdk, props.cameraID

    def test_full_frame_to_bin4_is_exact(self, camera):
        sdk, cid = camera
        sdk.set_image_bin(cid, 1)
        sdk.set_image_bin(cid, 4)
        assert sdk.get_image_size(cid) == (752, 752)

    def test_rounding_loss_compounds_through_bin3(self, camera):
        """The number that started this: 1 -> 3 -> 4 does NOT give 752x752."""
        sdk, cid = camera
        sdk.set_image_bin(cid, 1)
        sdk.set_image_bin(cid, 3)
        assert sdk.get_image_size(cid) == (1000, 1002), (
            "width rounds down to a multiple of 4"
        )
        sdk.set_image_bin(cid, 4)
        assert sdk.get_image_size(cid) == (748, 750), (
            "and the loss carries into the next bin"
        )

    def test_a_window_survives_a_bin_change_and_scales(self, camera):
        sdk, cid = camera
        sdk.set_image_bin(cid, 1)
        sdk.set_image_size(cid, 1024, 1024)
        sdk.set_image_start_pos(cid, 500, 500)
        sdk.set_image_bin(cid, 2)
        assert sdk.get_image_size(cid) == (512, 512)
        assert sdk.get_image_start_pos(cid) == (250, 250)

    def test_height_rounds_to_a_multiple_of_two(self, camera):
        """Undocumented: the header states only the width rule."""
        sdk, cid = camera
        sdk.set_image_bin(cid, 1)
        for requested, expected in [
            ((1024, 1024), (1024, 1024)),
            ((1023, 1024), (1020, 1024)),
            ((1021, 1024), (1020, 1024)),
            ((1024, 1023), (1024, 1022)),
            ((1024, 1021), (1024, 1020)),
        ]:
            sdk.set_image_size(cid, *requested)
            assert sdk.get_image_size(cid) == expected, f"for {requested}"


class TestConfigureRestoresFullFrame:
    """The driver-level consequence of the above, and the bug it caused."""

    def test_consecutive_configures_do_not_shrink_the_frame(self):
        from chimera_player_one.sdk.camera import Camera

        sdk = CameraSdk(FakeCameraLibrary([ARES_M_PRO]))
        with Camera.open(sdk) as cam:
            cam.configure(binning=3)
            geometry = cam.configure(binning=4)
            assert (geometry.width, geometry.height) == (752, 752), (
                "configure() must ask for full frame explicitly; binning alone "
                "inherits the previous ROI"
            )
