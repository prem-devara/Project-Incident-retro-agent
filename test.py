from dotenv import load_dotenv
load_dotenv()                       # reads your key from .env

import aisuite as ai
client = ai.Client()

response = client.chat.completions.create(
    model="anthropic:claude-haiku-5-5",
    messages=[{"role": "user", "content": "Say hello in five words."}],
    max_tokens=100,
)
print(response.choices[0].message.content)