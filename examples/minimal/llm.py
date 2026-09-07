"""Run twice with the same input; the second response comes from the cache."""

from cached_response import cached_llm_response


@cached_llm_response(mode="testing")
def ask_llm(request):
    """Stand-in for your model call: return text or JSON-compatible data."""
    print("Model function executed")
    return "Inspect the current state without changing it."


def main():
    # A long request meets the default 100-word minimum without extra settings.
    request = {
        "messages": [
            {
                "role": "user",
                "content": "Inspect current state without making any changes. "
                * 20,
            }
        ]
    }
    print(ask_llm(request))
    print(ask_llm(request))


if __name__ == "__main__":
    main()
