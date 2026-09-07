"""Cache a pure function using exact arguments, with no word minimum."""

from cached_response import cached_staticmethod


@cached_staticmethod
def square(value):
    print("Function executed")
    return value * value


def main():
    print(square(12))
    print(square(12))


if __name__ == "__main__":
    main()
