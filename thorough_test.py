import os
import json
from google import genai
from google.genai import types

os.environ["GCP_PROJECT_ID"] = "fleet-gift-498306-p7"
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "/Users/prishadureja/Desktop/aws-sportsfan360-sentiment/google_creds.json"

client = genai.Client(
    vertexai=True,
    project="fleet-gift-498306-p7",
    location="us-central1"
)

def run_thorough_test():
    print("=======================================")
    print("RIGOROUS E2E SCHEMA VALIDATION TEST")
    print("=======================================")
    
    # Test 1: Dolly Bot (Analysis Type)
    print("\n[1/3] Testing Dolly (Pre-Match Analysis)")
    prompt = """
    You are Dolly. Match: India vs England. Phase: Pre-Match.
    Generate a deep tactical breakdown.
    Format EXACTLY as this JSON:
    {
        "type": "analysis",
        "cardType": "analysis",
        "title": "Match Prediction",
        "bulletPoints": ["Point 1", "Point 2"]
    }
    """
    res1 = client.models.generate_content(
        model='gemini-2.5-flash', contents=prompt,
        config=types.GenerateContentConfig(temperature=0.7, response_mime_type="application/json")
    )
    
    data1 = json.loads(res1.text)
    if "type" in data1 and "cardType" in data1 and "bulletPoints" in data1 and type(data1["bulletPoints"]) == list:
        print("✅ Dolly output PASSED schema validation:")
        print(json.dumps(data1, indent=2))
    else:
        print("❌ Dolly schema FAILED")
        
    # Test 2: Krishna (Partisan Bot)
    print("\n[2/3] Testing Krishna (Partisan India Fan)")
    prompt2 = """
    You are Krishna, a biased India superfan.
    Context: Virat Kohli just hit a boundary.
    Generate 1 short, punchy chat message.
    Format EXACTLY as this JSON:
    {
        "type": "chat",
        "text": "Your message!"
    }
    """
    res2 = client.models.generate_content(
        model='gemini-2.5-flash', contents=prompt2,
        config=types.GenerateContentConfig(temperature=0.9, response_mime_type="application/json")
    )
    
    data2 = json.loads(res2.text)
    if data2.get("type") == "chat" and "text" in data2:
        print("✅ Krishna output PASSED schema validation:")
        print(json.dumps(data2, indent=2))
    else:
        print("❌ Krishna schema FAILED")
        
run_thorough_test()
