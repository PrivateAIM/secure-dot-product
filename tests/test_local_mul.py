from flame.star import StarModelTester

# import pytest
import random
from secureDotProduct.mpc_mul_local import BeaverMultiplicationAnalyzer, BeaverAggregator


def test_beaver_multiplication():
    numElements = 3
    secret_1 = [random.randint(1, 100) for _ in range(numElements)]  # dynamic random secret values for node 0
    secret_2 = [random.randint(1, 100) for _ in range(numElements)]  # dynamic random secret values for node 1

    StarModelTester(
        data_splits=[secret_1, secret_2],
        analyzer=BeaverMultiplicationAnalyzer,
        aggregator=BeaverAggregator,
        data_type="s3",
        simple_analysis=False,
    )

    expected = [a * b for a, b in zip(secret_1, secret_2)]
    print("Expected Result", expected)
    # assert result["final_product"] == pytest.approx(expected, abs=1e-4)
