from dotenv import load_dotenv
from groq import Groq

load_dotenv()

client = Groq()

print("Sending request to Groq...", flush=True)

response = client.chat.completions.create(
    model="openai/gpt-oss-20b",
    messages=[
        {
            "role": "user",
            "content": "Reply exactly with: GROQ API WORKING"
        }
    ],
    max_tokens=300
)

print("Finish reason:", response.choices[0].finish_reason)
print("Response:", repr(response.choices[0].message.content))