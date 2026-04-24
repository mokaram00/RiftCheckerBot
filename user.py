import json
import os
import telebot
from telebot import types

# the version of the json data we save to our telegram users's stats
# when something new gets added, we increase the version by 1
TELEGRAM_USER_DATA_VERSION = 2

class RiftUser:
    def __init__(self, userID: int, username: str):
        self.userID: int = userID
        self.username: str = username
        self.user_data: json = {}

    def register(self) -> dict:
        user_path = f"users/{self.userID}.json"  
        if os.path.exists(user_path):
            # user is already registered
            return {}
        
        os.makedirs(os.path.dirname(user_path), exist_ok=True)
        self.user_data = {
            'ID': self.userID,
            'username': self.username,
            'version': TELEGRAM_USER_DATA_VERSION,
            'accounts_checked': 0,
            'style': 0,
            'theme': 0,
            'gradient_type': 0,
            'alpha_tester_1_badge': False,
            'alpha_tester_2_badge': False,
            'alpha_tester_3_badge': False,
            'newbie_badge': False,
            'advanced_badge': False,
            'epic_badge': False,
            'alpha_tester_1_badge_active': False,
            'alpha_tester_2_badge_active': False,
            'alpha_tester_3_badge_active': False,
            'newbie_badge_active': False,
            'advanced_badge_active': False,
            'epic_badge_active': False,
            'saved_accounts': [],
            'vip': False,
        }
    
        with open(user_path, 'w') as user_data_file:
            json.dump(self.user_data, user_data_file, indent=4)
        
        return self.user_data

    def load_data(self) -> dict:
        # loading the telegram user profile's stats
        user_path = f"users/{self.userID}.json"
        if not os.path.exists(user_path):
            # profile not found
            return {}
        
        if os.path.getsize(user_path) > 0:
            # loading the user profile's stats
            with open(user_path, 'r') as user_data_file:
                self.user_data = json.load(user_data_file)

        # versioning, making sure there is no missing info to not mess the user data

        # returning the user data
        self.user_data['version'] = TELEGRAM_USER_DATA_VERSION
        if "theme" not in self.user_data:
            self.user_data["theme"] = self.user_data.get("style", 0)
        if "saved_accounts" not in self.user_data:
            self.user_data["saved_accounts"] = []
        if "fortnite_game_root" not in self.user_data:
            self.user_data["fortnite_game_root"] = ""
        if "vip" not in self.user_data:
            self.user_data["vip"] = False
        if "parental_pin_verify_day" not in self.user_data:
            self.user_data["parental_pin_verify_day"] = ""
        if "parental_pin_verify_count" not in self.user_data:
            self.user_data["parental_pin_verify_count"] = 0
        return self.user_data
    
    def update_data(self):
        # updating the telegram user profile's stats
        user_path = f"users/{self.userID}.json"
        if not os.path.exists(user_path):
            # user path doesn't exists, so we cannot update it
            return
        
        with open(user_path, 'w') as user_data_file:
            json.dump(self.user_data, user_data_file, indent=4)

    def upsert_saved_account(self, entry: dict) -> None:
        """Merge by ``account_id`` (Epic). Expects ``device_auth`` from ``create_device_auths``."""
        self.load_data()
        if "saved_accounts" not in self.user_data:
            self.user_data["saved_accounts"] = []
        lst: list = self.user_data["saved_accounts"]
        aid = (entry.get("account_id") or "").strip()
        if not aid:
            return
        for i, x in enumerate(lst):
            if isinstance(x, dict) and (x.get("account_id") or "").strip() == aid:
                lst[i] = entry
                self.update_data()
                return
        lst.append(entry)
        self.update_data()