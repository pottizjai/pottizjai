import os
import time
import re
import random
import threading
from datetime import datetime, timezone
from flask import Flask
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from google.auth.transport.requests import Request

# --- 1. Flask Web Server to Keep Render Awake ---
app = Flask('')

@app.route('/')
def home():
    return "YouTube Live Chat Bot is running smoothly!"

def run_web_server():
    app.run(host='0.0.0.0', port=8080)

# Start web server in background thread
threading.Thread(target=run_web_server, daemon=True).start()


# --- 2. Database & Multi-Project YouTube API Initialization ---
MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    print("Error: MONGO_URI environment variable is not set!", flush=True)
    exit(1)

db = MongoClient(MONGO_URI)["youtube_bot"]
token_collection = db["tokens"]
chat_history = db["processed_messages_v2"]

active_token_id = "youtube_auth_1"

def get_youtube_client(token_id):
    """Fetches credentials from MongoDB, refreshes if expired, and builds the YouTube client."""
    token_data = token_collection.find_one({"_id": token_id})
    
    if not token_data and token_id == "youtube_auth_1":
        token_data = token_collection.find_one({"_id": "youtube_auth"})
        if token_data:
            token_id = "youtube_auth"

    if not token_data:
        return None, None
    
    creds = Credentials(
        token=token_data["token"],
        refresh_token=token_data["refresh_token"],
        token_uri=token_data["token_uri"],
        client_id=token_data["client_id"],
        client_secret=token_data["client_secret"],
        scopes=token_data["scopes"],
    )

    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_collection.update_one(
                {"_id": token_id}, {"$set": {"token": creds.token}}
            )
        except Exception as e:
            print(f"Error refreshing token for {token_id}: {e}", flush=True)

    return build("youtube", "v3", credentials=creds), token_id

youtube, active_token_id = get_youtube_client("youtube_auth_1")
if not youtube:
    print("Error: No YouTube authentication tokens found in MongoDB for Project 1.", flush=True)
    exit(1)


def extract_video_id(url_or_id):
    """Extracts the 11-character video ID from any YouTube URL or raw ID."""
    if len(url_or_id) == 11 and "/" not in url_or_id and "?" not in url_or_id:
        return url_or_id
    patterns = [
        r'(?:v=|\/)([0-9A-Za-z_-]{11}).*',
        r'(?:embed\/)([0-9A-Za-z_-]{11})',
        r'(?:watch\?v=)([0-9A-Za-z_-]{11})',
        r'(?:youtu\.be\/)([0-9A-Za-z_-]{11})'
    ]
    for pattern in patterns:
        match = re.search(pattern, url_or_id)
        if match:
            return match.group(1)
    return url_or_id


def is_quota_error(error_message):
    err_lower = error_message.lower()
    return "quota" in err_lower and ("exceeded" in err_lower or "limit" in err_lower or "403" in err_lower)


def switch_to_project_2():
    global youtube, active_token_id
    print("⚠️ Quota exhausted! Automatically switching over to Project 2...", flush=True)
    new_client, new_id = get_youtube_client("youtube_auth_2")
    if new_client:
        youtube = new_client
        active_token_id = new_id
        print("✅ Successfully switched to Project 2!", flush=True)
        return True
    else:
        print("❌ Project 2 tokens ('youtube_auth_2') not found in MongoDB. Cannot failover.", flush=True)
        return False


# --- 3. Main Bot Logic ---
def main():
    global youtube, active_token_id
    print("Bot is starting up...", flush=True)

    bot_channel_id = None
    bot_display_name = None
    try:
        channels_response = youtube.channels().list(part="snippet", mine=True).execute()
        items = channels_response.get("items", [])
        if items:
            bot_channel_id = items[0]["id"]
            bot_display_name = items[0]["snippet"]["title"]
            print(f"Authenticated as: {bot_display_name} (Channel ID: {bot_channel_id}) using [{active_token_id}]", flush=True)
    except Exception as e:
        print(f"Warning: Could not fetch bot profile details: {e}", flush=True)

    stream_input = os.getenv("STREAM_INPUT")
    if not stream_input:
        print("Error: STREAM_INPUT environment variable is not set!", flush=True)
        return
    
    stream_input = stream_input.strip()
    video_id = extract_video_id(stream_input)
    print(f"Target Video ID: {video_id}", flush=True)

    live_chat_id = None
    try:
        video_response = youtube.videos().list(
            part="liveStreamingDetails,snippet",
            id=video_id
        ).execute()

        items = video_response.get("items", [])
        if not items:
            print("Error: Video ID could not be found on YouTube.", flush=True)
            return

        item = items[0]
        print(f"Stream Title: {item.get('snippet', {}).get('title')}", flush=True)

        if "liveStreamingDetails" not in item or "activeLiveChatId" not in item["liveStreamingDetails"]:
            print("Error: No active live chat found for this video.", flush=True)
            return

        live_chat_id = item["liveStreamingDetails"]["activeLiveChatId"]
        print(f"Successfully connected to Live Chat ID: {live_chat_id}\n", flush=True)

    except Exception as e:
        print(f"Error fetching live stream details: {e}", flush=True)
        return

    try:
        chat_history.create_index("msg_id", unique=True)
        print("MongoDB unique index on 'msg_id' verified.", flush=True)
    except Exception as e:
        print(f"Index note: {e}", flush=True)

    estimated_units_used = 0
    last_reset_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    shutdown_sent = False
    user_cooldowns = {}
    COOLDOWN_SECONDS = 10

    # --- 4. Initialization: Skip existing backlog completely ---
    print("Initializing chat sync and clearing past backlog...", flush=True)
    page_token = None
    
    try:
        # Initial call to grab the current live head token without processing old messages
        initial_response = youtube.liveChatMessages().list(
            liveChatId=live_chat_id,
            part="snippet,authorDetails",
            maxResults=200
        ).execute()
        
        estimated_units_used += 5
        page_token = initial_response.get("nextPageToken")
        messages = initial_response.get("items", [])
        
        # Seed MongoDB with current backlog so they are marked as seen
        for msg in messages:
            try:
                chat_history.insert_one({"msg_id": msg["id"]})
            except Exception:
                pass
                
        print(f"Startup sync complete! Seeded {len(messages)} past messages. Now listening for NEW live chat messages...", flush=True)
    except Exception as e:
        print(f"Error during initial sync: {e}", flush=True)

    polling_interval_ms = 5000

    # --- 5. Continuous Polling Loop ---
    while True:
        try:
            current_day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if current_day != last_reset_day:
                estimated_units_used = 0
                last_reset_day = current_day
                shutdown_sent = False

            kwargs = {
                "liveChatId": live_chat_id,
                "part": "snippet,authorDetails",
                "maxResults": 15
            }
            if page_token:
                kwargs["pageToken"] = page_token

            current_time_str = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
            print(f"[{current_time_str}] 🔍 Checking chat messages via [{active_token_id}]...", flush=True)

            chat_response = youtube.liveChatMessages().list(**kwargs).execute()
            estimated_units_used += 5

            messages = chat_response.get("items", [])
            polling_interval_ms = chat_response.get("pollingIntervalMillis", 5000)
            
            # Only update page_token if a new one is provided by YouTube
            new_page_token = chat_response.get("nextPageToken")
            if new_page_token:
                page_token = new_page_token

            for msg in messages:
                msg_id = msg["id"]
                text = msg["snippet"]["displayMessage"]
                author = msg["authorDetails"]["displayName"]
                author_channel_id = msg["authorDetails"].get("channelId")

                # 1. Ignore bot's own messages
                if bot_channel_id and author_channel_id == bot_channel_id:
                    continue
                if bot_display_name and author.lower() == bot_display_name.lower():
                    continue

                # 2. Database-level duplicate prevention
                try:
                    chat_history.insert_one({"msg_id": msg_id})
                except DuplicateKeyError:
                    continue  
                except Exception:
                    continue

                # Log brand-new message found in chat
                print(f"💬 New Chat Message -> [{author}]: {text}", flush=True)

                # --- 6. Command Handling ---
                reply_text = None
                clean_text = text.lower().strip()

                if clean_text.startswith("!"):
                    last_time = user_cooldowns.get(author, 0)
                    if time.time() - last_time < COOLDOWN_SECONDS:
                        continue
                    user_cooldowns[author] = time.time()

                    if clean_text == "!discord" or clean_text.startswith("!discord "):
                        reply_text = "Join our community Discord server here: discord.gg/jai"
                    elif clean_text == "!coinflip" or clean_text.startswith("!coinflip "):
                        outcome = random.choice(["Heads", "Tails"])
                        reply_text = f"🪙 Coin Flip Result for {author}: {outcome}!"
                    elif clean_text == "!8ball" or clean_text.startswith("!8ball "):
                        responses = [
                            "Yes, definitely!", "As I see it, yes.", "Reply hazy, try again.",
                            "Cannot predict now.", "Don't count on it.", "My sources say no.",
                            "Outlook not so good.", "Very doubtful."
                        ]
                        reply_text = f"🔮 Magic 8-Ball says to {author}: {random.choice(responses)}"
                    elif clean_text == "!pp" or clean_text.startswith("!pp "):
                        size = random.randint(1, 100)
                        reply_text = f"Your pp is {size} inches"

                # --- 7. Send Reply ---
                if reply_text:
                    try:
                        youtube.liveChatMessages().insert(
                            part="snippet",
                            body={
                                "snippet": {
                                    "liveChatId": live_chat_id,
                                    "type": "textMessageEvent",
                                    "textMessageDetails": {"messageText": reply_text},
                                }
                            },
                        ).execute()
                        estimated_units_used += 50
                        print(f"🤖 Replied to {author}: \"{reply_text}\"", flush=True)
                    except Exception as e:
                        print(f"Error sending YouTube reply: {e}", flush=True)

            safe_sleep_time = max(polling_interval_ms / 1000.0, 12.0)
            time.sleep(safe_sleep_time)

        except Exception as e:
            error_message = str(e)
            if is_quota_error(error_message) and active_token_id in ["youtube_auth_1", "youtube_auth"]:
                if switch_to_project_2():
                    estimated_units_used = 0
                    shutdown_sent = False
                    continue

            print(f"Error in polling loop: {e}", flush=True)
            time.sleep(10)


if __name__ == "__main__":
    main()
