from bank import withdraw


def test_withdraw_happy_path():
    assert withdraw(100, 30) == 70
