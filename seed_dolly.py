import os
from firebase_admin import firestore
from firebase_store import init_firebase

def seed_dolly_profile():
    print("⏳ Seeding Dolly Bot profile in Firestore...")
    db = init_firebase()
    
    dolly_ref = db.collection("users").document("dolly-dolphin-bot")
    
    import base64
    from PIL import Image
    import io
    
    image_path = "/Users/prishadureja/.gemini/antigravity/brain/569d7e98-522f-474e-b6f7-f981cda3210d/media__1782465265511.png"
    try:
        # Resize image to 150x150 for avatar
        img = Image.open(image_path)
        img = img.resize((150, 150), Image.Resampling.LANCZOS)
        
        buffered = io.BytesIO()
        img.save(buffered, format="PNG")
        encoded_string = base64.b64encode(buffered.getvalue()).decode("utf-8")
        avatar_url = f"data:image/png;base64,{encoded_string}"
    except Exception as e:
        print(f"⚠️ Failed to load or resize local image: {e}. Using fallback path.")
        avatar_url = "/images/dolly.png"
    
    bio_text = """🐬 Meet Dolly
Hi, I'm Dolly! 👋
"Your AI Sports Buddy. Always in the Game."
The official AI mascot of SportsFan360.

I live and breathe sports, bringing fans together through live matches, predictions, stats, trivia, communities, and real-time conversations. Whether you need match insights, breaking news, or just someone to celebrate a last-ball win with—I'm always here.

Let's play. Let's cheer. Let's roar. 💙🏆"""

    dolly_data = {
        "userId": "dolly-dolphin-bot",
        "username": "Dolly",
        "firstName": "Dolly",
        "lastName": "Dolphin",
        "badge": "RISING_FAN",
        "avatarUrl": avatar_url,
        "bio": bio_text,
        "about": bio_text,
        "description": bio_text,
        "sports": ["cricket", "football"],
        "teams": ["India"]
    }
    
    dolly_ref.set(dolly_data, merge=True)
    print("✅ Dolly Bot profile seeded successfully (users/dolly-dolphin-bot)!")

if __name__ == "__main__":
    seed_dolly_profile()
