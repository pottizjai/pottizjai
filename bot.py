import os
import time
import re
import random
import threading
from datetime import datetime
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
    return "YouTube Live Chat Bot is running smoothly with Multi-Project Failover & Enhanced Logging!"

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
    
    # Fallback support if Project 1 was originally saved as "youtube_auth"
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

# Initialize with Project 1
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
    """Robust helper to check if any error string indicates a quota limit issue."""
    err_lower = error_message.lower()
    return "quota" in err_lower and ("exceeded" in err_lower or "limit" in err_lower or "403" in err_lower)


def switch_to_project_2():
    """Helper function to switch YouTube client to Project 2."""
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

    # Fetch authenticated bot identity with startup failover check
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
        if is_quota_error(str(e)) and active_token_id in ["youtube_auth_1", "youtube_auth"]:
            if switch_to_project_2():
                try:
                    channels_response = youtube.channels().list(part="snippet", mine=True).execute()
                    items = channels_response.get("items", [])
                    if items:
                        bot_channel_id = items[0]["id"]
                        bot_display_name = items[0]["snippet"]["title"]
                        print(f"Authenticated as: {bot_display_name} (Channel ID: {bot_channel_id}) using [{active_token_id}]", flush=True)
                except Exception as inner_e:
                    print(f"Warning: Could not fetch bot profile details on Project 2: {inner_e}", flush=True)
        else:
            print(f"Warning: Could not fetch bot profile details: {e}", flush=True)

    # Get stream input from environment variables
    stream_input = os.getenv("STREAM_INPUT")
    if not stream_input:
        print("Error: STREAM_INPUT environment variable is not set!", flush=True)
        return
    
    stream_input = stream_input.strip()
    video_id = extract_video_id(stream_input)
    print(f"Target Video ID: {video_id}", flush=True)

    # Fetch live chat ID from video details with startup failover check
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
            print("Error: No active live chat found for this video. Ensure it is live and chat is enabled.", flush=True)
            return

        live_chat_id = item["liveStreamingDetails"]["activeLiveChatId"]
        print(f"Successfully connected to Live Chat ID: {live_chat_id}\n", flush=True)

    except Exception as e:
        if is_quota_error(str(e)) and active_token_id in ["youtube_auth_1", "youtube_auth"]:
            if switch_to_project_2():
                try:
                    video_response = youtube.videos().list(
                        part="liveStreamingDetails,snippet",
                        id=video_id
                    ).execute()
                    items = video_response.get("items", [])
                    if items:
                        item = items[0]
                        print(f"Stream Title: {item.get('snippet', {}).get('title')}", flush=True)
                        if "liveStreamingDetails" in item and "activeLiveChatId" in item["liveStreamingDetails"]:
                            live_chat_id = item["liveStreamingDetails"]["activeLiveChatId"]
                            print(f"Successfully connected to Live Chat ID on Project 2: {live_chat_id}\n", flush=True)
                except Exception as inner_e:
                    print(f"Error fetching live stream details on Project 2: {inner_e}", flush=True)
                    return
        if not live_chat_id:
            print(f"Error fetching live stream details: {e}", flush=True)
            return

    # Ensure unique index exists on MongoDB collection for duplicate prevention
    try:
        chat_history.create_index("msg_id", unique=True)
        print("MongoDB unique index on 'msg_id' verified.", flush=True)
    except Exception as e:
        print(f"Index note: {e}", flush=True)

    # Quota tracker, shutdown flag, and user cooldown dictionaries
    estimated_units_used = 0
    last_reset_day = datetime.utcnow().strftime("%Y-%m-%d")
    shutdown_sent = False
    user_cooldowns = {}  # author -> timestamp of last command
    COOLDOWN_SECONDS = 10

    print("Initializing chat sync (ignoring past messages)...", flush=True)

    # --- 4. Continuous Polling Loop ---
    page_token = None
    is_startup = True

    while True:
        try:
            # Reset local quota tracker if the day changed
            current_day = datetime.utcnow().strftime("%Y-%m-%d")
            if current_day != last_reset_day:
                estimated_units_used = 0
                last_reset_day = current_day
                shutdown_sent = False
                print("New day detected: Local quota tracker reset.", flush=True)

            # Quota Safety Check: If quota is nearing 10,000 units on current project
            if estimated_units_used >= 9900 and not shutdown_sent:
                shutdown_sent = True
                try:
                    youtube.liveChatMessages().insert(
                        part="snippet",
                        body={
                            "snippet": {
                                "liveChatId": live_chat_id,
                                "type": "textMessageEvent",
                                "textMessageDetails": {
                                    "messageText": "Quota exhausted for project! Switching or shutting down."
                                },
                            }
                        },
                    ).execute()
                except Exception:
                    pass

            kwargs = {
                "liveChatId": live_chat_id,
                "part": "snippet,authorDetails",
                "maxResults": 15
            }
            if page_token:
                kwargs["pageToken"] = page_token

            # Log check for chat
            current_time_str = datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')
            print(f"[{current_time_str}] 🔍 Checking chat messages via [{active_token_id}]...", flush=True)

            chat_response = youtube.liveChatMessages().list(**kwargs).execute()
            
            # Each list request costs 5 quota units
            estimated_units_used += 5
            quota_remaining = max(0, 10000 - estimated_units_used)

            messages = chat_response.get("items", [])
            polling_interval_ms = chat_response.get("pollingIntervalMillis", 5000)
            page_token = chat_response.get("nextPageToken")

            for msg in messages:
                msg_id = msg["id"]
                text = msg["snippet"]["displayMessage"]
                author = msg["authorDetails"]["displayName"]
                author_channel_id = msg["authorDetails"].get("channelId")

                # If this is the startup fetch, just seed the DB with existing history and DO NOT reply
                if is_startup:
                    try:
                        chat_history.insert_one({"msg_id": msg_id})
                    except (DuplicateKeyError, Exception):
                        pass
                    continue

                # 1. Prevent infinite loops by ignoring messages sent by the bot itself
                if bot_channel_id and author_channel_id == bot_channel_id:
                    continue
                if bot_display_name and author.lower() == bot_display_name.lower():
                    continue

                # 2. Strict Database-level duplicate prevention
                try:
                    chat_history.insert_one({"msg_id": msg_id})
                except DuplicateKeyError:
                    continue  
                except Exception:
                    continue

                # Log new message found in chat
                print(f"💬 New Chat Message -> [{author}]: {text}", flush=True)

                # --- 5. Command Handling & User Cooldowns ---
                reply_text = None
                clean_text = text.lower().strip()

                if clean_text.startswith("!"):
                    # Check per-user cooldown (10 seconds)
                    last_time = user_cooldowns.get(author, 0)
                    if time.time() - last_time < COOLDOWN_SECONDS:
                        continue  # Ignore if user is spamming commands
                    user_cooldowns[author] = time.time()

                    if clean_text == "!discord" or clean_text.startswith("!discord "):
                        reply_text = "Join our community Discord server here: discord.gg/example"
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

                # --- 6. Send Reply to YouTube & Track Quota ---
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
                        # Each insert request costs 50 quota units
                        estimated_units_used += 50
                        quota_remaining = max(0, 10000 - estimated_units_used)
                        
                        # Enhanced Reply & Quota Log
                        print(f"🤖 Replied to {author}: \"{reply_text}\" | Quota Used: {estimated_units_used}/10000 | Quota Remaining: {quota_remaining} (Token: {active_token_id})", flush=True)
                    except Exception as e:
                        print(f"Error sending YouTube reply: {e}", flush=True)

            # After the first batch is processed, disable startup mode
            if is_startup:
                is_startup = False
                print("Startup sync complete! Now actively listening for NEW live chat messages...\n", flush=True)

            # Safe polling rate floor (12 seconds)
            safe_sleep_time = max(polling_interval_ms / 1000.0, 12.0)
            time.sleep(safe_sleep_time)

        except Exception as e:
            error_message = str(e)
            
            # AUTOMATIC FAILOVER: Catch any variation of quota exceeded and switch to Project 2
            if is_quota_error(error_message) and active_token_id in ["youtube_auth_1", "youtube_auth"]:
                if switch_to_project_2():
                    estimated_units_used = 0  # Reset local tracker for Project 2 quota
                    shutdown_sent = False
                    print("Resuming live chat loop with Project 2...", flush=True)
                    continue  # Retry loop immediately with Project 2

            print(f"Error in polling loop: {e}", flush=True)
            time.sleep(10)


if __name__ == "__main__":
    main()
