
# from openai import OpenAI, AuthenticationError

# def test_openai_api_key(api_key: str) -> bool:
#     try:
        
#         client = OpenAI(api_key=api_key)
#         client.models.list()  # Simple authenticated request
#         return True
#     except AuthenticationError as e:
#         print(f"API key validation failed: {e}")
#         return False
#     except Exception as e:
#         print(f"Unexpected error: {e}")
#         return False

# if test_openai_api_key(api_key):
#     print("Valid API key")
# else:
#     print("Invalid API key")

from openai import OpenAI

MODELS_TO_TEST = ["gpt-5.5", "gpt-5.2", "gpt-5.3-codex"]

def test_model_access(client: OpenAI, model: str) -> tuple[bool, str]:
    try:
        response = client.responses.create(
            model=model,
            input="Reply with just the word: ok",
            max_output_tokens=16,
        )
        return True, response.output_text
    except Exception as e:
        return False, str(e)

def main(api_key: str):
    client = OpenAI(api_key=api_key)
    for model in MODELS_TO_TEST:
        ok, detail = test_model_access(client, model)
        print(f"{model}: {'accessible' if ok else 'NOT accessible'}")
        if not ok:
            print(f"   -> {detail}")

if __name__ == "__main__":
    main(api_key)