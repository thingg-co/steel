"""Tests for engine helpers: clean, _temper and _calibrate."""
import collections
import pytest
import types
import numpy as np

import steel.engine as eng
from steel.engine import Engine


class TestClean:
    """Tests for the clean() function."""

    def test_clean_makes_json_safe(self):
        """clean() converts numpy types, NaN/inf to None, tuples/deques to lists."""
        result = eng.clean({
            "a": np.float64(1.5),
            "b": float("nan"),
            "c": np.int64(3),
            "d": np.bool_(True),
            "e": (1, 2),
            "f": collections.deque([np.float32("inf")]),
            "g": np.array([1.0, 2.0]),
            7: "x",
        })
        assert result == {
            "a": 1.5,
            "b": None,
            "c": 3,
            "d": True,
            "e": [1, 2],
            "f": [None],
            "g": [1.0, 2.0],
            "7": "x",
        }

    def test_clean_string_and_none(self):
        """clean() returns strings and None unchanged."""
        assert eng.clean("s") == "s"
        assert eng.clean(None) is None


class TestTemper:
    """Tests for Engine._temper()."""

    def test_temper_identity_with_p_scale_1(self):
        """With p_scale 1.0, _temper is essentially identity."""
        fake = types.SimpleNamespace(
            metrics=types.SimpleNamespace(p_scale=1.0, band_scale=1.0),
            cfg=types.SimpleNamespace(),
        )
        assert Engine._temper(fake, 0.7) == pytest.approx(0.7, abs=1e-6)

    def test_temper_all_0_5_with_p_scale_0(self):
        """With p_scale 0.0, every input gives 0.5."""
        fake = types.SimpleNamespace(
            metrics=types.SimpleNamespace(p_scale=0.0, band_scale=1.0),
            cfg=types.SimpleNamespace(),
        )
        assert Engine._temper(fake, 0.7) == pytest.approx(0.5, abs=1e-6)
        assert Engine._temper(fake, 0.3) == pytest.approx(0.5, abs=1e-6)
        assert Engine._temper(fake, 0.0) == pytest.approx(0.5, abs=1e-6)
        assert Engine._temper(fake, 1.0) == pytest.approx(0.5, abs=1e-6)

    def test_temper_sharpened_with_p_scale_2(self):
        """With p_scale 2.0, inputs are sharpened."""
        fake = types.SimpleNamespace(
            metrics=types.SimpleNamespace(p_scale=2.0, band_scale=1.0),
            cfg=types.SimpleNamespace(),
        )
        # p=0.7 should be > 0.7, p=0.3 should be < 0.3
        assert Engine._temper(fake, 0.7) > 0.7
        assert Engine._temper(fake, 0.3) < 0.3

    def test_temper_extremes_do_not_raise(self):
        """_temper(0.0) and _temper(1.0) do not raise and stay within (0, 1)."""
        fake = types.SimpleNamespace(
            metrics=types.SimpleNamespace(p_scale=1.0, band_scale=1.0),
            cfg=types.SimpleNamespace(),
        )
        result_0 = Engine._temper(fake, 0.0)
        result_1 = Engine._temper(fake, 1.0)
        assert 0 < result_0 < 1
        assert 0 < result_1 < 1


class TestCalibrate:
    """Tests for Engine._calibrate()."""

    def test_calibrate_scales_bands_around_median(self):
        """Calibration scales bands around the median q[2]."""
        # Mock _temper as a standalone lambda since _calibrate calls it
        def temper_mock(p):
            return p

        fake = types.SimpleNamespace(
            metrics=types.SimpleNamespace(p_scale=1.0, band_scale=2.0),
            cfg=types.SimpleNamespace(),
            _temper=temper_mock,
        )
        q = [-20, -10, 0, 10, 20]
        qc, p = Engine._calibrate(fake, q, 0.6)
        # With band_scale=2.0, distances from median 0 are doubled
        # [-20-0, -10-0, 0, 10-0, 20-0] * 2 + 0 = [-40, -20, 0, 20, 40]
        assert qc == pytest.approx([-40, -20, 0, 20, 40], abs=1e-6)
        # p equals _temper(p_raw) since band_scale doesn't affect p
        assert p == pytest.approx(0.6, abs=1e-6)

    def test_calibrate_with_median_pivot(self):
        """With q=[90,95,100,105,110] and band_scale=0.5, median is pivot."""
        def temper_mock(p):
            return p

        fake = types.SimpleNamespace(
            metrics=types.SimpleNamespace(p_scale=1.0, band_scale=0.5),
            cfg=types.SimpleNamespace(),
            _temper=temper_mock,
        )
        q = [90, 95, 100, 105, 110]
        qc, p = Engine._calibrate(fake, q, 0.5)
        # With band_scale=0.5, distances from median 100 are halved
        # [90-100, 95-100, 100, 105-100, 110-100] * 0.5 + 100
        # = [-10*0.5+100, -5*0.5+100, 100, 5*0.5+100, 10*0.5+100]
        # = [95, 97.5, 100, 102.5, 105]
        assert qc == pytest.approx([95, 97.5, 100, 102.5, 105], abs=1e-6)
