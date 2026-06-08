import pytest
from calculator import add, subtract, multiply, divide


class TestAdd:
    def test_positive_numbers(self):
        assert add(2, 3) == 5

    def test_negative_numbers(self):
        assert add(-1, -1) == -2

    def test_mixed_signs(self):
        assert add(5, -3) == 2

    def test_zero(self):
        assert add(0, 0) == 0
        assert add(0, 7) == 7


class TestSubtract:
    def test_positive_numbers(self):
        assert subtract(5, 3) == 2

    def test_negative_numbers(self):
        assert subtract(-1, -1) == 0

    def test_negative_result(self):
        assert subtract(3, 5) == -2

    def test_zero(self):
        assert subtract(0, 0) == 0
        assert subtract(7, 0) == 7


class TestMultiply:
    def test_positive_numbers(self):
        assert multiply(3, 4) == 12

    def test_negative_numbers(self):
        assert multiply(-3, 4) == -12

    def test_double_negative(self):
        assert multiply(-3, -4) == 12

    def test_zero(self):
        assert multiply(0, 5) == 0
        assert multiply(5, 0) == 0


class TestDivide:
    def test_positive_numbers(self):
        assert divide(10, 2) == 5

    def test_negative_numbers(self):
        assert divide(-10, 2) == -5

    def test_float_result(self):
        assert divide(7, 2) == 3.5

    def test_division_by_zero(self):
        with pytest.raises(ValueError):
            divide(5, 0)

    def test_zero_dividend(self):
        assert divide(0, 5) == 0
