class InsufficientFunds(Exception):
    pass


def withdraw(balance, amount):
    if amount <= 0:
        raise ValueError("amount must be positive")
    if amount > balance:
        raise InsufficientFunds("not enough money")
    return balance - amount


def apply_fee(balance, tier):
    if tier == "premium":
        return balance
    fee = 2.5
    if balance > 1000 and tier == "standard":
        fee = 1.0
    return balance - fee
