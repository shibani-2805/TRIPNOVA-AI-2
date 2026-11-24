"""
TripNova - travel_planner.py
============================

The trip-planning engine behind TripNova.

This file contains your original AITravelPlanner class. Your destination,
activity, restaurant, hotel and travel-tip data are kept exactly as you wrote
them. What changed:

  * The console `input()` menus were replaced by `validate_trip_request()`,
    which checks the values that arrive from the website form.
  * `get_weather()` no longer uses random numbers. It returns a clearly
    labelled SEASONAL ESTIMATE (or a real forecast when one is available).
  * Costs are now converted between currencies correctly.
  * `generate_itinerary()` no longer shuffles your attraction lists in place
    (the old code silently re-ordered your data), and it returns data that the
    website can display instead of printing text.

HOW THE PLANNER WORKS (all rule-based, no AI model is used here):
  1. Every day is assigned to one of the chosen destinations.
  2. Activities are picked from the categories the user selected.
  3. Attractions, restaurants and hotels come from the data tables below.
  4. Costs = destination daily budget + activity prices, per person, times the
     number of travellers.
"""

import json
import os
import random
import re
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

# ----------------------------------------------------------------------
# SETTINGS YOU CAN EDIT
# ----------------------------------------------------------------------

MAX_DAYS = 30
MAX_TRAVELLERS = 20
MAX_FUTURE_DAYS = 730          # how far ahead a trip may start (about 2 years)

BUDGET_TYPES = ("budget", "mid-range", "luxury")

# Your activity prices are written as plain numbers. They are treated as
# prices in INR (the base currency) and converted to each destination's
# currency, so a "300" activity is never added to a EUR/JPY budget as-is.
BASE_CURRENCY = "INR"

# International activities usually cost more than the sample INR prices.
# This multiplier is applied to activity prices for International
# destinations only. Set it to 1 to switch it off.
INTERNATIONAL_ACTIVITY_PRICE_FACTOR = 4

CURRENCIES = {
    "INR": {"symbol": "₹", "name": "Indian Rupee"},
    "USD": {"symbol": "$", "name": "US Dollar"},
    "EUR": {"symbol": "€", "name": "Euro"},
    "JPY": {"symbol": "¥", "name": "Japanese Yen"},
    "THB": {"symbol": "฿", "name": "Thai Baht"},
    "IDR": {"symbol": "Rp", "name": "Indonesian Rupiah"},
}

# Units of each currency that 1 INR buys. These are APPROXIMATE numbers used
# only when the live exchange-rate service cannot be reached. Update them if
# you want better offline estimates.
FALLBACK_RATES_PER_INR = {
    "INR": 1.0,
    "USD": 0.0114,
    "EUR": 0.0097,
    "JPY": 1.70,
    "THB": 0.37,
    "IDR": 187.0,
}

# Extra information used by the website. This does NOT change your data
# tables; it only adds map coordinates (for the optional live forecast) and
# the Wikipedia page title used to fetch a destination photo.
DESTINATION_EXTRAS = {
    "Ooty": {"lat": 11.4102, "lon": 76.6950, "image": "Ooty"},
    "Munnar": {"lat": 10.0889, "lon": 77.0595, "image": "Munnar"},
    "Goa": {"lat": 15.4909, "lon": 73.8278, "image": "Goa"},
    "Jaipur": {"lat": 26.9124, "lon": 75.7873, "image": "Jaipur"},
    "New Delhi": {"lat": 28.6139, "lon": 77.2090, "image": "New_Delhi"},
    "Manali": {"lat": 32.2396, "lon": 77.1887, "image": "Manali,_Himachal_Pradesh"},
    "Kashmir": {"lat": 34.0837, "lon": 74.7973, "image": "Kashmir"},
    "Mysuru": {"lat": 12.2958, "lon": 76.6394, "image": "Mysore"},
    "Hyderabad": {"lat": 17.3850, "lon": 78.4867, "image": "Hyderabad"},
    "Kochi": {"lat": 9.9312, "lon": 76.2673, "image": "Kochi"},
    "Varanasi": {"lat": 25.3176, "lon": 82.9739, "image": "Varanasi"},
    "Paris": {"lat": 48.8566, "lon": 2.3522, "image": "Paris"},
    "Tokyo": {"lat": 35.6762, "lon": 139.6503, "image": "Tokyo"},
    "Rome": {"lat": 41.9028, "lon": 12.4964, "image": "Rome"},
    "Bali": {"lat": -8.5069, "lon": 115.2625, "image": "Bali"},
    "New York": {"lat": 40.7128, "lon": -74.0060, "image": "New_York_City"},
    "Bangkok": {"lat": 13.7563, "lon": 100.5018, "image": "Bangkok"},
}

# Typical-conditions text used with the seasonal estimate.
SEASON_CONDITIONS = {
    "Winter": "Cool, mostly dry",
    "Summer": "Warm to hot",
    "Monsoon": "Rain showers likely",
    "Autumn": "Mild and pleasant",
}


class ValidationError(Exception):
    """Raised when the trip form contains invalid values.

    `errors` is a dictionary like {"days": "Please enter ..."} so the website
    can show each message next to the right form field.
    """

    def __init__(self, errors):
        super().__init__("Invalid trip request")
        self.errors = errors


def _to_int(value):
    """Convert form input to a whole number or raise ValueError."""
    if isinstance(value, bool):
        raise ValueError("boolean is not a number")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"-?\d{1,9}", value.strip()):
        return int(value.strip())
    raise ValueError("not a whole number")


def round_money(amount, currency):
    """USD and EUR keep cents; the other currencies use whole numbers."""
    if currency in ("USD", "EUR"):
        return round(float(amount), 2)
    return int(round(float(amount)))


def describe_weather_code(code):
    """Turn an Open-Meteo (WMO) weather code into plain words."""
    if code is None:
        return "Mixed conditions"
    if code == 0:
        return "Clear sky"
    if code == 1:
        return "Mostly clear"
    if code == 2:
        return "Partly cloudy"
    if code == 3:
        return "Overcast"
    if code in (45, 48):
        return "Fog"
    if 51 <= code <= 57:
        return "Drizzle"
    if 61 <= code <= 67:
        return "Rain"
    if 71 <= code <= 77:
        return "Snow"
    if 80 <= code <= 82:
        return "Rain showers"
    if code in (85, 86):
        return "Snow showers"
    if 95 <= code <= 99:
        return "Thunderstorm"
    return "Mixed conditions"


class AITravelPlanner:
    """Rule-based trip planner (see the notes at the top of this file)."""

    # Small in-memory caches so we do not call outside services too often.
    _forecast_cache = {}
    _rates_cache = {
        "time": 0.0,
        "rates": None,
        "source": "fallback",
        "retry_after": 0.0,
    }

    def __init__(self):

        # ============================================================
        # DESTINATION DATABASE
        # ============================================================

        self.destinations = {

            # -------------------- DOMESTIC --------------------

            "Ooty": {
                "country": "India",
                "state": "Tamil Nadu",
                "type": "Domestic",
                "currency": "INR",
                "daily_budget": {
                    "budget": 1500,
                    "mid-range": 3000,
                    "luxury": 6000
                },
                "attractions": [
                    "Ooty Lake",
                    "Botanical Garden",
                    "Doddabetta Peak",
                    "Rose Garden",
                    "Nilgiri Mountain Railway"
                ]
            },

            "Munnar": {
                "country": "India",
                "state": "Kerala",
                "type": "Domestic",
                "currency": "INR",
                "daily_budget": {
                    "budget": 1500,
                    "mid-range": 3000,
                    "luxury": 6000
                },
                "attractions": [
                    "Tea Gardens",
                    "Eravikulam National Park",
                    "Mattupetty Dam",
                    "Top Station",
                    "Echo Point"
                ]
            },

            "Goa": {
                "country": "India",
                "state": "Goa",
                "type": "Domestic",
                "currency": "INR",
                "daily_budget": {
                    "budget": 1500,
                    "mid-range": 3000,
                    "luxury": 6000
                },
                "attractions": [
                    "Baga Beach",
                    "Calangute Beach",
                    "Fort Aguada",
                    "Basilica of Bom Jesus",
                    "Dudhsagar Falls"
                ]
            },

            "Jaipur": {
                "country": "India",
                "state": "Rajasthan",
                "type": "Domestic",
                "currency": "INR",
                "daily_budget": {
                    "budget": 1500,
                    "mid-range": 3000,
                    "luxury": 6000
                },
                "attractions": [
                    "Amber Fort",
                    "City Palace",
                    "Hawa Mahal",
                    "Jantar Mantar",
                    "Jal Mahal"
                ]
            },

            "New Delhi": {
                "country": "India",
                "state": "Delhi",
                "type": "Domestic",
                "currency": "INR",
                "daily_budget": {
                    "budget": 1500,
                    "mid-range": 3000,
                    "luxury": 6000
                },
                "attractions": [
                    "India Gate",
                    "Red Fort",
                    "Qutub Minar",
                    "Lotus Temple",
                    "Humayun's Tomb"
                ]
            },

            "Manali": {
                "country": "India",
                "state": "Himachal Pradesh",
                "type": "Domestic",
                "currency": "INR",
                "daily_budget": {
                    "budget": 1500,
                    "mid-range": 3000,
                    "luxury": 6000
                },
                "attractions": [
                    "Solang Valley",
                    "Rohtang Pass",
                    "Hadimba Temple",
                    "Old Manali",
                    "Manali Nature Park"
                ]
            },

            "Kashmir": {
                "country": "India",
                "state": "Jammu and Kashmir",
                "type": "Domestic",
                "currency": "INR",
                "daily_budget": {
                    "budget": 1500,
                    "mid-range": 3000,
                    "luxury": 6000
                },
                "attractions": [
                    "Dal Lake",
                    "Gulmarg",
                    "Pahalgam",
                    "Sonamarg",
                    "Mughal Gardens"
                ]
            },

            "Mysuru": {
                "country": "India",
                "state": "Karnataka",
                "type": "Domestic",
                "currency": "INR",
                "daily_budget": {
                    "budget": 1500,
                    "mid-range": 3000,
                    "luxury": 6000
                },
                "attractions": [
                    "Mysore Palace",
                    "Chamundi Hills",
                    "Brindavan Gardens",
                    "Mysore Zoo",
                    "St. Philomena's Church"
                ]
            },

            "Hyderabad": {
                "country": "India",
                "state": "Telangana",
                "type": "Domestic",
                "currency": "INR",
                "daily_budget": {
                    "budget": 1500,
                    "mid-range": 3000,
                    "luxury": 6000
                },
                "attractions": [
                    "Charminar",
                    "Golconda Fort",
                    "Hussain Sagar Lake",
                    "Salar Jung Museum",
                    "Ramoji Film City"
                ]
            },

            "Kochi": {
                "country": "India",
                "state": "Kerala",
                "type": "Domestic",
                "currency": "INR",
                "daily_budget": {
                    "budget": 1500,
                    "mid-range": 3000,
                    "luxury": 6000
                },
                "attractions": [
                    "Fort Kochi",
                    "Chinese Fishing Nets",
                    "Mattancherry Palace",
                    "Marine Drive",
                    "Jewish Synagogue"
                ]
            },

            "Varanasi": {
                "country": "India",
                "state": "Uttar Pradesh",
                "type": "Domestic",
                "currency": "INR",
                "daily_budget": {
                    "budget": 1500,
                    "mid-range": 3000,
                    "luxury": 6000
                },
                "attractions": [
                    "Ganges River Ghats",
                    "Kashi Vishwanath Temple",
                    "Sarnath",
                    "Dashashwamedh Ghat",
                    "Assi Ghat"
                ]
            },

            # -------------------- INTERNATIONAL --------------------

            "Paris": {
                "country": "France",
                "state": "Île-de-France",
                "type": "International",
                "currency": "EUR",
                "daily_budget": {
                    "budget": 100,
                    "mid-range": 150,
                    "luxury": 300
                },
                "attractions": [
                    "Eiffel Tower",
                    "Louvre Museum",
                    "Arc de Triomphe",
                    "Notre-Dame",
                    "Montmartre"
                ]
            },

            "Tokyo": {
                "country": "Japan",
                "state": "Tokyo",
                "type": "International",
                "currency": "JPY",
                "daily_budget": {
                    "budget": 10000,
                    "mid-range": 18000,
                    "luxury": 35000
                },
                "attractions": [
                    "Tokyo Tower",
                    "Shibuya Crossing",
                    "Senso-ji Temple",
                    "Meiji Shrine",
                    "Tokyo Skytree"
                ]
            },

            "Rome": {
                "country": "Italy",
                "state": "Lazio",
                "type": "International",
                "currency": "EUR",
                "daily_budget": {
                    "budget": 100,
                    "mid-range": 160,
                    "luxury": 300
                },
                "attractions": [
                    "Colosseum",
                    "Trevi Fountain",
                    "Vatican City",
                    "Pantheon",
                    "Roman Forum"
                ]
            },

            "Bali": {
                "country": "Indonesia",
                "state": "Bali",
                "type": "International",
                "currency": "IDR",
                "daily_budget": {
                    "budget": 1000000,
                    "mid-range": 1800000,
                    "luxury": 3500000
                },
                "attractions": [
                    "Uluwatu Temple",
                    "Ubud",
                    "Tegallalang Rice Terrace",
                    "Tanah Lot",
                    "Seminyak Beach"
                ]
            },

            "New York": {
                "country": "USA",
                "state": "New York",
                "type": "International",
                "currency": "USD",
                "daily_budget": {
                    "budget": 100,
                    "mid-range": 180,
                    "luxury": 350
                },
                "attractions": [
                    "Statue of Liberty",
                    "Times Square",
                    "Central Park",
                    "Brooklyn Bridge",
                    "Empire State Building"
                ]
            },

            "Bangkok": {
                "country": "Thailand",
                "state": "Bangkok",
                "type": "International",
                "currency": "THB",
                "daily_budget": {
                    "budget": 1500,
                    "mid-range": 2500,
                    "luxury": 5000
                },
                "attractions": [
                    "Grand Palace",
                    "Wat Arun",
                    "Wat Pho",
                    "Chatuchak Market",
                    "Chao Phraya River"
                ]
            }
        }

        # ============================================================
        # ACTIVITIES
        # ============================================================

        self.activities = {

            "Outdoor": [
                ("Sightseeing", 300),
                ("Nature Walk", 200),
                ("Photography", 150),
                ("City Tour", 400),
                ("Garden Visit", 200)
            ],

            "Cultural": [
                ("Museum Visit", 300),
                ("Heritage Walk", 250),
                ("Temple Visit", 100),
                ("Historical Tour", 350),
                ("Cultural Show", 500)
            ],

            "Adventure": [
                ("Trekking", 500),
                ("Boating", 400),
                ("Mountain Adventure", 800),
                ("Cycling", 300),
                ("Wildlife Safari", 700)
            ],

            "Relaxation": [
                ("Spa", 800),
                ("Beach Relaxation", 100),
                ("Resort Relaxation", 200),
                ("Yoga", 250),
                ("Leisure Walk", 100)
            ]
        }

        # ============================================================
        # FOOD / RESTAURANTS
        # ============================================================

        self.restaurants = {

            "budget": [
                "Local Food Restaurant",
                "Street Food Centre",
                "Budget Family Restaurant",
                "Local Café"
            ],

            "mid-range": [
                "Popular Local Restaurant",
                "Family Dining Restaurant",
                "Multi-Cuisine Restaurant",
                "City View Restaurant"
            ],

            "luxury": [
                "Luxury Fine-Dining Restaurant",
                "Premium Hotel Restaurant",
                "Rooftop Fine Dining",
                "Premium Multi-Cuisine Restaurant"
            ]
        }

        # ============================================================
        # HOTEL DATABASE
        # ============================================================

        self.hotels = {

            "Ooty": {
                "budget": {
                    "name": "Fortune Retreats Ooty",
                    "website": "https://www.fortuneretreat.com/"
                },
                "mid-range": {
                    "name": "Fortune Resort Sullivan Court",
                    "website": "https://www.itchotels.com/in/en/fortuneresortsullivancourt-ooty"
                },
                "luxury": {
                    "name": "Kluney Manor",
                    "website": "https://www.kluneymanor.com/"
                }
            },

            "Munnar": {
                "budget": {
                    "name": "Sun Mount View",
                    "website": "https://www.sunmountview.com/"
                },
                "mid-range": {
                    "name": "T&U Leisure Hotel",
                    "website": "https://maximunnar.com/leisure-hotel/"
                },
                "luxury": {
                    "name": "Ragamaya Resort & Spa",
                    "website": "https://www.ragamayamunnar.com/"
                }
            },

            "Goa": {
                "budget": {
                    "name": "Fortune Miramar Goa",
                    "website": "https://www.itchotels.com/in/en/fortunemiramar-goa"
                },
                "mid-range": {
                    "name": "Fortune Resort Benaulim Goa",
                    "website": "https://www.itchotels.com/in/en/fortuneresort-benaulim-goa"
                },
                "luxury": {
                    "name": "Taj Exotica Resort & Spa Goa",
                    "website": "https://www.tajhotels.com/en-in/hotels/taj-exotica-goa"
                }
            },

            "Jaipur": {
                "budget": {
                    "name": "Hotel Pearl Palace",
                    "website": "https://www.hotelpearlpalace.com/"
                },
                "mid-range": {
                    "name": "Shahpura House",
                    "website": "https://www.shahpura.com/"
                },
                "luxury": {
                    "name": "Raffles Jaipur",
                    "website": "https://www.raffles.com/jaipur/"
                }
            },

            "New Delhi": {
                "budget": {
                    "name": "Hotel Hari Piorko",
                    "website": "https://www.hotelharipiorko.com/"
                },
                "mid-range": {
                    "name": "Fortune Park East Delhi",
                    "website": "https://www.itchotels.com/in/en/fortuneparkeastdelhi"
                },
                "luxury": {
                    "name": "Le Méridien New Delhi",
                    "website": "https://www.marriott.com/en-us/hotels/delmd-le-meridien-new-delhi/overview/"
                }
            },

            "Manali": {
                "budget": {
                    "name": "Hotel Greenfields Manali",
                    "website": "https://www.hotelgreenfields.com/"
                },
                "mid-range": {
                    "name": "Snow Valley Resorts Manali",
                    "website": "https://snowvalleyresorts.com/"
                },
                "luxury": {
                    "name": "Storii By ITC Hotels Urvashis Retreat",
                    "website": "https://www.itchotels.com/"
                }
            },

            "Kashmir": {
                "budget": {
                    "name": "Hotel Ahdoos Srinagar",
                    "website": "https://www.hotelahdoos.com/"
                },
                "mid-range": {
                    "name": "Fortune Resort Heevan Srinagar",
                    "website": "https://www.itchotels.com/"
                },
                "luxury": {
                    "name": "The Lalit Grand Palace Srinagar",
                    "website": "https://www.thelalit.com/the-lalit-grand-palace-srinagar/"
                }
            },

            "Mysuru": {
                "budget": {
                    "name": "Hotel Roopa",
                    "website": "https://www.hotelroopa.com/"
                },
                "mid-range": {
                    "name": "Fortune JP Palace Mysore",
                    "website": "https://www.itchotels.com/in/en/fortunejppalace-mysore"
                },
                "luxury": {
                    "name": "Radisson Blu Plaza Hotel Mysore",
                    "website": "https://www.radissonhotels.com/en-us/hotels/radisson-blu-plaza-mysore"
                }
            },

            "Hyderabad": {
                "budget": {
                    "name": "Hotel Vaishnaoi",
                    "website": "https://hotelvaishnaoi.com/"
                },
                "mid-range": {
                    "name": "ITC Kakatiya",
                    "website": "https://www.itchotels.com/in/en/itckakatiya-hyderabad"
                },
                "luxury": {
                    "name": "ITC Kohenur",
                    "website": "https://www.itchotels.com/in/en/itckohenur-hyderabad"
                }
            },

            "Kochi": {
                "budget": {
                    "name": "Fort Kochi Beach Inn",
                    "website": "https://www.fortkochiinn.com/"
                },
                "mid-range": {
                    "name": "KTDC Bolgatty Palace",
                    "website": "https://www.ktdc.com/bolgatty-palace"
                },
                "luxury": {
                    "name": "Grand Hyatt Kochi Bolgatty",
                    "website": "https://www.hyatt.com/grand-hyatt/en-US/cokgh-grand-hyatt-kochi-bolgatty"
                }
            },

            "Varanasi": {
                "budget": {
                    "name": "Hotel Surya Kaiser Palace",
                    "website": "https://www.hotelsuryavaranasi.com/"
                },
                "mid-range": {
                    "name": "Ramada Plaza by Wyndham JHV Varanasi",
                    "website": "https://www.wyndhamhotels.com/ramada/varanasi-india/ramada-plaza-jhv-varanasi/overview"
                },
                "luxury": {
                    "name": "Taj Ganges Varanasi",
                    "website": "https://www.tajhotels.com/en-in/hotels/taj-ganges-varanasi"
                }
            },

            "Paris": {
                "budget": {
                    "name": "ibis budget Paris Porte de Montmartre",
                    "website": "https://all.accor.com/hotel/5094/index.en.shtml"
                },
                "mid-range": {
                    "name": "Novotel Paris Centre Tour Eiffel",
                    "website": "https://all.accor.com/hotel/3543/index.en.shtml"
                },
                "luxury": {
                    "name": "Le Meurice",
                    "website": "https://www.dorchestercollection.com/paris/le-meurice/"
                }
            },

            "Tokyo": {
                "budget": {
                    "name": "Hotel Gracery Shinjuku",
                    "website": "https://shinjuku.gracery.com/"
                },
                "mid-range": {
                    "name": "Hotel Metropolitan Tokyo Ikebukuro",
                    "website": "https://hotel-metropolitan.com/ikebukuro/"
                },
                "luxury": {
                    "name": "The Peninsula Tokyo",
                    "website": "https://www.peninsula.com/en/tokyo/5-star-luxury-hotel-ginza"
                }
            },

            "Rome": {
                "budget": {
                    "name": "Hotel Scott House",
                    "website": "https://www.hotelscothouse.com/"
                },
                "mid-range": {
                    "name": "Hotel Artemide",
                    "website": "https://www.hotelartemide.it/"
                },
                "luxury": {
                    "name": "Hotel de Russie",
                    "website": "https://www.roccofortehotels.com/hotels-and-resorts/hotel-de-russie/"
                }
            },

            "Bali": {
                "budget": {
                    "name": "Puri Garden Hotel & Hostel",
                    "website": "https://www.purigardenhotel.com/"
                },
                "mid-range": {
                    "name": "Anumana Ubud Hotel",
                    "website": "https://www.anumanaubud.com/"
                },
                "luxury": {
                    "name": "Four Seasons Resort Bali at Sayan",
                    "website": "https://www.fourseasons.com/sayan/"
                }
            },

            "New York": {
                "budget": {
                    "name": "Pod 51",
                    "website": "https://www.thepodhotel.com/pod-51/"
                },
                "mid-range": {
                    "name": "Moxy NYC Times Square",
                    "website": "https://www.marriott.com/en-us/hotels/nycot-moxy-nyc-times-square/overview/"
                },
                "luxury": {
                    "name": "The Plaza New York",
                    "website": "https://www.theplazany.com/"
                }
            },

            "Bangkok": {
                "budget": {
                    "name": "ibis Bangkok Riverside",
                    "website": "https://all.accor.com/hotel/6512/index.en.shtml"
                },
                "mid-range": {
                    "name": "Novotel Bangkok Platinum Pratunam",
                    "website": "https://all.accor.com/hotel/6128/index.en.shtml"
                },
                "luxury": {
                    "name": "Mandarin Oriental Bangkok",
                    "website": "https://www.mandarinoriental.com/en/bangkok/chao-phraya-river"
                }
            }
        }

    # ============================================================
    # DATA FOR THE WEBSITE
    # (replaces display_destinations() and the printed menus)
    # ============================================================

    def get_destination_list(self):
        """Destinations as plain data for the website."""

        result = []

        for name, data in self.destinations.items():

            extra = DESTINATION_EXTRAS.get(name, {})

            result.append({
                "name": name,
                "state": data["state"],
                "country": data["country"],
                "type": data["type"],
                "currency": data["currency"],
                "attractions": list(data["attractions"]),
                "daily_budget": dict(data["daily_budget"]),
                "image_title": extra.get("image", name),
                "tip": self.get_tip(name)
            })

        return result

    def get_category_list(self):
        """Activity categories as plain data for the website."""

        return [
            {
                "name": category,
                "count": len(items),
                "activities": [
                    {"name": name, "cost_inr": cost}
                    for name, cost in items
                ]
            }
            for category, items in self.activities.items()
        ]

    # ============================================================
    # VALIDATION
    # (replaces get_number_of_destinations(), get_destinations(),
    #  get_number_of_days(), get_budget_type(), get_number_of_activities(),
    #  get_activity_categories(), get_start_date(), get_travellers())
    # ============================================================

    def validate_trip_request(self, data, today=None):
        """Check the form values and return a clean dictionary.

        Raises ValidationError with one message per bad field.
        """

        if not isinstance(data, dict):
            raise ValidationError(
                {"_form": "The request must be a JSON object."}
            )

        errors = {}
        clean = {}
        today = today or date.today()

        # ---------------- destinations ----------------
        raw = data.get("destinations")

        if not isinstance(raw, list) or not raw:
            errors["destinations"] = (
                "Please select at least one destination."
            )
        else:
            names = []

            for item in raw:

                if not isinstance(item, str) or item not in self.destinations:
                    errors["destinations"] = (
                        "One of the selected destinations is not available."
                    )
                    break

                if item in names:
                    errors["destinations"] = (
                        "Please do not select the same destination twice."
                    )
                    break

                names.append(item)

            else:
                clean["destinations"] = names

        # ---------------- start date ----------------
        raw_date = data.get("start_date")

        try:
            if not isinstance(raw_date, str):
                raise ValueError

            start = datetime.strptime(raw_date.strip(), "%Y-%m-%d").date()

            # one day of slack so users in time zones ahead of the
            # server are not rejected for choosing "today"
            if start < today - timedelta(days=1):
                errors["start_date"] = "The start date cannot be in the past."
            elif start > today + timedelta(days=MAX_FUTURE_DAYS):
                errors["start_date"] = (
                    "Please choose a start date within the next two years."
                )
            else:
                clean["start_date"] = start

        except ValueError:
            errors["start_date"] = "Please choose a valid start date."

        # ---------------- days ----------------
        try:
            days = _to_int(data.get("days"))

            if days < 1 or days > MAX_DAYS:
                errors["days"] = (
                    f"Trip length must be between 1 and {MAX_DAYS} days."
                )
            else:
                clean["days"] = days

        except ValueError:
            errors["days"] = "Please enter the number of days as a whole number."

        # one destination per day at minimum
        if "destinations" in clean and "days" in clean:

            if clean["days"] < len(clean["destinations"]):
                errors["days"] = (
                    f"You selected {len(clean['destinations'])} destinations, "
                    f"so you need at least {len(clean['destinations'])} days."
                )
                clean.pop("days", None)

        # ---------------- travellers ----------------
        try:
            travellers = _to_int(data.get("travellers"))

            if travellers < 1 or travellers > MAX_TRAVELLERS:
                errors["travellers"] = (
                    f"Travellers must be between 1 and {MAX_TRAVELLERS}."
                )
            else:
                clean["travellers"] = travellers

        except ValueError:
            errors["travellers"] = (
                "Please enter the number of travellers as a whole number."
            )

        # ---------------- budget ----------------
        budget = data.get("budget_type")

        if isinstance(budget, str):
            budget = budget.strip().lower()

        if budget in BUDGET_TYPES:
            clean["budget_type"] = budget
        else:
            errors["budget_type"] = (
                "Please choose Budget, Mid-range or Luxury."
            )

        # ---------------- activity categories ----------------
        raw = data.get("categories")
        categories = []

        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, str) and item in self.activities:
                    if item not in categories:
                        categories.append(item)
                else:
                    categories = []
                    break

        if categories:
            clean["categories"] = categories
        else:
            errors["categories"] = (
                "Please select at least one activity preference."
            )

        # ---------------- number of activities ----------------
        try:
            number = _to_int(data.get("number_of_activities"))

            if number < 1:
                errors["number_of_activities"] = (
                    "Please choose at least 1 activity."
                )
            elif "categories" in clean:

                available = sum(
                    len(self.activities[c]) for c in clean["categories"]
                )

                if number > available:
                    errors["number_of_activities"] = (
                        f"Your chosen preferences have only {available} "
                        f"different activities. Choose {available} or fewer, "
                        f"or add another preference."
                    )
                else:
                    clean["number_of_activities"] = number

            else:
                # categories are invalid; that error is already reported
                clean["number_of_activities"] = number

        except ValueError:
            errors["number_of_activities"] = (
                "Please enter the number of activities as a whole number."
            )

        # ---------------- display currency ----------------
        currency = data.get("display_currency", BASE_CURRENCY)

        if isinstance(currency, str):
            currency = currency.strip().upper() or BASE_CURRENCY

        if currency in CURRENCIES:
            clean["display_currency"] = currency
        else:
            errors["display_currency"] = "Please choose a supported currency."

        if errors:
            raise ValidationError(errors)

        return clean

    # ============================================================
    # SEASON, HOTEL, DAILY COST, TIPS, ACTIVITY PICKER
    # (your original methods, unchanged)
    # ============================================================

    # ============================================================
    # SEASON
    # ============================================================

    def get_season(self, month):

        if month in [12, 1, 2]:
            return "Winter"

        elif month in [3, 4, 5]:
            return "Summer"

        elif month in [6, 7, 8, 9]:
            return "Monsoon"

        else:
            return "Autumn"

    # ============================================================
    # HOTEL
    # ============================================================

    def get_hotel(
        self,
        destination,
        budget_type
    ):

        return self.hotels[
            destination
        ][budget_type]

    # ============================================================
    # DAILY COST
    # ============================================================

    def calculate_daily_cost(
        self,
        destination,
        budget_type,
        activity_cost
    ):

        destination_data = self.destinations[
            destination
        ]

        base_cost = destination_data[
            "daily_budget"
        ][budget_type]

        return base_cost + activity_cost

    # ============================================================
    # TRAVEL TIP
    # ============================================================

    def get_tip(self, destination):

        tips = {

            "Ooty":
                "Carry warm clothes and enjoy the Nilgiri scenery.",

            "Munnar":
                "Carry comfortable shoes for tea gardens and nature walks.",

            "Goa":
                "Carry sunscreen, comfortable clothes and stay hydrated.",

            "Jaipur":
                "Carry water and comfortable footwear for forts.",

            "New Delhi":
                "Use comfortable transport and plan major attractions early.",

            "Manali":
                "Carry warm clothes and check mountain-road conditions.",

            "Kashmir":
                "Carry warm layers and plan hill travel carefully.",

            "Mysuru":
                "Plan palace and heritage visits during daytime.",

            "Hyderabad":
                "Try local cuisine and keep time for historical attractions.",

            "Kochi":
                "Explore Fort Kochi and keep an umbrella during rainy periods.",

            "Varanasi":
                "Wear comfortable footwear and respect local customs.",

            "Paris":
                "Use public transport and keep important belongings secure.",

            "Tokyo":
                "Use public transport and keep your travel card ready.",

            "Rome":
                "Wear comfortable shoes because many attractions involve walking.",

            "Bali":
                "Carry light clothing and keep a rain jacket available.",

            "New York":
                "Use public transport and plan attractions by neighbourhood.",

            "Bangkok":
                "Carry water and use public transport for major attractions."
        }

        return tips.get(
            destination,
            "Plan your day carefully and keep essential items with you."
        )

    # ============================================================
    # GET ACTIVITY FOR DAY
    # ============================================================

    def get_activity_for_day(
        self,
        activity_pool,
        used_activities,
        activity_index
    ):

        # First use unique activities
        available = [
            activity
            for activity in activity_pool
            if activity["name"] not in used_activities
        ]

        if available:

            activity = available[
                activity_index % len(available)
            ]

            used_activities.add(
                activity["name"]
            )

            return activity

        # If all activities are already used,
        # allow repetition
        return activity_pool[
            activity_index % len(activity_pool)
        ]


    # ============================================================
    # SMALL HELPERS
    # ============================================================

    @staticmethod
    def _flag(name, default="true"):
        """Read a true/false setting from the environment."""
        return os.environ.get(name, default).strip().lower() in (
            "1", "true", "yes", "on"
        )

    @staticmethod
    def _http_get_json(url, timeout=4):
        request = urllib.request.Request(
            url, headers={"User-Agent": "TripNova/1.0"}
        )

        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    # ============================================================
    # WEATHER
    # The old version used random numbers and presented them as weather.
    # This version is honest about what it is:
    #   * "seasonal_estimate": a typical range for the season, from your
    #     temperature table. It is NOT a forecast.
    #   * "live_forecast": a real forecast from Open-Meteo (free, no API
    #     key). Only available for dates up to 15 days ahead.
    # ============================================================

    def get_weather(self, destination, day):

        data = self.destinations[destination]

        if data["type"] == "Domestic":

            temperature_ranges = {

                "Winter": (10, 22),
                "Summer": (20, 35),
                "Monsoon": (18, 30),
                "Autumn": (18, 30)
            }

        else:

            temperature_ranges = {

                "Winter": (5, 18),
                "Summer": (18, 32),
                "Monsoon": (18, 30),
                "Autumn": (10, 25)
            }

        season = self.get_season(day.month)

        low, high = temperature_ranges[season]

        return {
            "source": "seasonal_estimate",
            "label": "Seasonal estimate (not a forecast)",
            "season": season,
            "low": low,
            "high": high,
            "condition": SEASON_CONDITIONS[season]
        }

    def fetch_live_forecast(self, destination, first_day, last_day):
        """Real forecast from Open-Meteo. Returns {} if unavailable."""

        extra = DESTINATION_EXTRAS.get(destination)

        if not extra:
            return {}

        key = (destination, first_day.isoformat(), last_day.isoformat())
        cached = self._forecast_cache.get(key)

        if cached and time.time() - cached[0] < 1800:
            return cached[1]

        query = urllib.parse.urlencode({
            "latitude": extra["lat"],
            "longitude": extra["lon"],
            "daily": "weather_code,temperature_2m_max,temperature_2m_min",
            "timezone": "auto",
            "start_date": first_day.isoformat(),
            "end_date": last_day.isoformat()
        })

        result = {}

        try:
            payload = self._http_get_json(
                "https://api.open-meteo.com/v1/forecast?" + query
            )

            daily = payload["daily"]

            for day, code, high, low in zip(
                daily["time"],
                daily["weather_code"],
                daily["temperature_2m_max"],
                daily["temperature_2m_min"]
            ):
                if high is None or low is None:
                    continue

                result[day] = {
                    "source": "live_forecast",
                    "label": "Live forecast (Open-Meteo)",
                    "season": None,
                    "low": round(low),
                    "high": round(high),
                    "condition": describe_weather_code(code)
                }

        except Exception:
            # No internet, service down, or unexpected reply:
            # the caller falls back to the seasonal estimate.
            result = {}

        self._forecast_cache[key] = (time.time(), result)

        return result

    # ============================================================
    # CURRENCY
    # ============================================================

    def get_exchange_rates(self):
        """Return ({currency: units per 1 INR}, "live" or "fallback")."""

        now = time.time()
        cache = self._rates_cache

        if self._flag("USE_LIVE_RATES"):

            if cache["rates"] and now - cache["time"] < 6 * 3600:
                return dict(cache["rates"]), cache["source"]

            if now >= cache["retry_after"]:

                try:
                    payload = self._http_get_json(
                        "https://open.er-api.com/v6/latest/INR"
                    )

                    if payload.get("result") == "success":

                        rates = {
                            code: float(payload["rates"][code])
                            for code in CURRENCIES
                        }
                        rates["INR"] = 1.0

                        cache.update(
                            time=now, rates=rates,
                            source="live", retry_after=0.0
                        )

                        return dict(rates), "live"

                except Exception:
                    pass

                # do not hammer the service if it is unreachable
                cache["retry_after"] = now + 600

        return dict(FALLBACK_RATES_PER_INR), "fallback"

    @staticmethod
    def convert(amount, source, target, rates):
        """Convert money. `rates` = units of each currency per 1 INR."""

        if source == target:
            return amount

        return amount / rates[source] * rates[target]

    def activity_cost_in_local_currency(self, destination, cost_inr, rates):
        """Activity prices are INR numbers; convert to the destination."""

        data = self.destinations[destination]

        factor = 1

        if data["type"] == "International":
            factor = INTERNATIONAL_ACTIVITY_PRICE_FACTOR

        return self.convert(
            cost_inr * factor, BASE_CURRENCY, data["currency"], rates
        )

    # ============================================================
    # CREATE ACTIVITY POOL
    # (same as before, but uses a seeded random generator so a saved
    #  trip can be rebuilt exactly)
    # ============================================================

    def create_activity_pool(
        self,
        selected_categories,
        rng=None
    ):

        rng = rng or random.Random()

        activity_pool = []

        for category in selected_categories:

            for activity, cost in self.activities[category]:

                activity_pool.append(
                    {
                        "category": category,
                        "name": activity,
                        "cost": cost
                    }
                )

        rng.shuffle(activity_pool)

        return activity_pool

    # ============================================================
    # GENERATE DYNAMIC ITINERARY
    # ============================================================

    def generate_itinerary(
        self,
        destinations,
        budget_type,
        selected_categories,
        start_date,
        travellers,
        number_of_days,
        number_of_activities,
        rng=None,
        rates=None,
        display_currency=BASE_CURRENCY
    ):

        rng = rng or random.Random()

        if rates is None:
            rates = self.get_exchange_rates()[0]

        today = date.today()

        itinerary = []

        activity_pool = self.create_activity_pool(
            selected_categories,
            rng
        )

        used_activities = set()

        # User-selected activity count is used
        # as the preference pool
        preferred_activities = []

        for i in range(number_of_activities):

            activity = activity_pool[
                i % len(activity_pool)
            ]

            preferred_activities.append(
                activity
            )

        # ----------------------------------------------------
        # WHICH DESTINATION EACH DAY BELONGS TO
        # ----------------------------------------------------

        day_destinations = []

        for day_number in range(1, number_of_days + 1):

            destination_index = (
                (day_number - 1)
                * len(destinations)
                // number_of_days
            )

            day_destinations.append(
                destinations[
                    destination_index
                    % len(destinations)
                ]
            )

        # ----------------------------------------------------
        # LIVE FORECASTS (only for dates up to 15 days ahead)
        # ----------------------------------------------------

        live_forecasts = {}

        if self._flag("USE_LIVE_WEATHER"):

            for destination in destinations:

                dates = [
                    start_date + timedelta(days=index)
                    for index, name in enumerate(day_destinations)
                    if name == destination
                ]

                window = [
                    d for d in dates
                    if today <= d <= today + timedelta(days=15)
                ]

                if window:
                    live_forecasts[destination] = self.fetch_live_forecast(
                        destination, min(window), max(window)
                    )

        # ----------------------------------------------------
        # ATTRACTION ORDER PER DESTINATION
        # A shuffled COPY is used so your data is never re-ordered,
        # and attractions do not repeat until all have been used.
        # ----------------------------------------------------

        attraction_queues = {}

        for destination in destinations:

            order = list(
                self.destinations[destination]["attractions"]
            )

            rng.shuffle(order)

            attraction_queues[destination] = {
                "order": order,
                "position": 0
            }

        for day_number in range(
            1,
            number_of_days + 1
        ):

            current_date = (
                start_date +
                timedelta(
                    days=day_number - 1
                )
            )

            destination = day_destinations[day_number - 1]

            destination_data = self.destinations[
                destination
            ]

            currency = destination_data["currency"]

            hotel = dict(
                self.get_hotel(
                    destination,
                    budget_type
                )
            )

            weather = live_forecasts.get(
                destination, {}
            ).get(current_date.isoformat())

            if weather is None:
                weather = self.get_weather(
                    destination,
                    current_date
                )

            # ----------------------------------------------------
            # ACTIVITIES
            # ----------------------------------------------------

            activity1 = self.get_activity_for_day(
                preferred_activities,
                used_activities,
                (day_number - 1) * 3
            )

            activity2 = self.get_activity_for_day(
                preferred_activities,
                used_activities,
                (day_number - 1) * 3 + 1
            )

            activity3 = self.get_activity_for_day(
                preferred_activities,
                used_activities,
                (day_number - 1) * 3 + 2
            )

            # ----------------------------------------------------
            # ATTRACTIONS
            # ----------------------------------------------------

            queue = attraction_queues[destination]
            order = queue["order"]

            attraction1 = order[
                queue["position"] % len(order)
            ]

            attraction2 = order[
                (queue["position"] + 1) % len(order)
            ]

            queue["position"] += 2

            # ----------------------------------------------------
            # FOOD (three different suggestions per day)
            # ----------------------------------------------------

            breakfast, lunch, dinner = rng.sample(
                self.restaurants[budget_type],
                3
            )

            # ----------------------------------------------------
            # COST (all in the destination's own currency)
            # ----------------------------------------------------

            slot_costs = [
                self.activity_cost_in_local_currency(
                    destination, activity["cost"], rates
                )
                for activity in (activity1, activity2, activity3)
            ]

            activity_cost = sum(slot_costs)

            daily_cost = self.calculate_daily_cost(
                destination,
                budget_type,
                activity_cost
            )

            total_day_cost = (
                daily_cost * travellers
            )

            per_person = round_money(daily_cost, currency)
            group_total = round_money(total_day_cost, currency)

            group_total_display = round_money(
                self.convert(
                    group_total, currency, display_currency, rates
                ),
                display_currency
            )

            # ----------------------------------------------------
            # DAY PLAN
            # ----------------------------------------------------

            day_plan = {

                "day": day_number,

                "date":
                    current_date.isoformat(),

                "date_display":
                    current_date.strftime(
                        "%d-%m-%Y"
                    ),

                "weekday":
                    current_date.strftime(
                        "%A"
                    ),

                "destination":
                    destination,

                "weather":
                    weather,

                "hotel":
                    hotel,

                "morning": {
                    "activity":
                        activity1["name"],
                    "category":
                        activity1["category"],
                    "attraction":
                        attraction1,
                    "cost":
                        round_money(slot_costs[0], currency)
                },

                "afternoon": {
                    "activity":
                        activity2["name"],
                    "category":
                        activity2["category"],
                    "attraction":
                        attraction2,
                    "cost":
                        round_money(slot_costs[1], currency)
                },

                "evening": {
                    "activity":
                        activity3["name"],
                    "category":
                        activity3["category"],
                    "cost":
                        round_money(slot_costs[2], currency)
                },

                "food": {
                    "breakfast":
                        breakfast,
                    "lunch":
                        lunch,
                    "dinner":
                        dinner
                },

                "costs": {
                    "currency":
                        currency,
                    "base_per_person":
                        round_money(
                            destination_data["daily_budget"][budget_type],
                            currency
                        ),
                    "activities_per_person":
                        round_money(activity_cost, currency),
                    "per_person":
                        per_person,
                    "group_total":
                        group_total,
                    "display_currency":
                        display_currency,
                    "group_total_display":
                        group_total_display
                },

                "estimated_cost_per_person":
                    per_person,

                "estimated_total_cost":
                    group_total
            }

            itinerary.append(
                day_plan
            )

        return itinerary

    # ============================================================
    # GENERATE A COMPLETE TRIP (itinerary + summary)
    # This replaces display_itinerary() and run(): instead of printing,
    # it returns data that Flask sends to the website as JSON.
    # ============================================================

    def generate_trip(self, request, seed=None):
        """`request` must come from validate_trip_request()."""

        if seed is None:
            seed = random.SystemRandom().randrange(1, 2 ** 31)

        rng = random.Random(seed)

        destinations = request["destinations"]
        budget_type = request["budget_type"]
        travellers = request["travellers"]
        start_date = request["start_date"]
        number_of_days = request["days"]
        display_currency = request["display_currency"]

        rates, rates_source = self.get_exchange_rates()

        itinerary = self.generate_itinerary(
            destinations,
            budget_type,
            request["categories"],
            start_date,
            travellers,
            number_of_days,
            request["number_of_activities"],
            rng=rng,
            rates=rates,
            display_currency=display_currency
        )

        # ---------------- totals ----------------

        totals = {}
        days_per_currency = {}

        for day in itinerary:

            currency = day["costs"]["currency"]

            totals[currency] = (
                totals.get(currency, 0)
                + day["costs"]["group_total"]
            )

            days_per_currency[currency] = (
                days_per_currency.get(currency, 0) + 1
            )

        grand_total = sum(
            self.convert(amount, currency, display_currency, rates)
            for currency, amount in totals.items()
        )

        totals_by_currency = [
            {
                "currency": currency,
                "days": days_per_currency[currency],
                "total": round_money(amount, currency),
                "per_person": round_money(
                    amount / travellers, currency
                )
            }
            for currency, amount in totals.items()
        ]

        forecast_days = sum(
            1 for day in itinerary
            if day["weather"]["source"] == "live_forecast"
        )

        summary = {
            "display_currency": display_currency,
            "grand_total": round_money(grand_total, display_currency),
            "per_person": round_money(
                grand_total / travellers, display_currency
            ),
            "per_day": round_money(
                grand_total / number_of_days, display_currency
            ),
            "totals_by_currency": totals_by_currency,
            "rates_source": rates_source,
            "forecast_days": forecast_days,
            "total_days": number_of_days
        }

        # ---------------- destination cards ----------------

        trip_destinations = []

        for name in destinations:

            data = self.destinations[name]

            trip_destinations.append({
                "name": name,
                "state": data["state"],
                "country": data["country"],
                "type": data["type"],
                "currency": data["currency"],
                "attractions": list(data["attractions"]),
                "hotel": dict(self.get_hotel(name, budget_type)),
                "tip": self.get_tip(name),
                "image_title": DESTINATION_EXTRAS.get(
                    name, {}
                ).get("image", name),
                "days": [
                    day["day"] for day in itinerary
                    if day["destination"] == name
                ]
            })

        end_date = start_date + timedelta(days=number_of_days - 1)

        return {
            "seed": seed,
            "generated_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            ),
            "request": {
                "destinations": destinations,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "days": number_of_days,
                "travellers": travellers,
                "budget_type": budget_type,
                "categories": request["categories"],
                "number_of_activities": request["number_of_activities"],
                "display_currency": display_currency
            },
            "destinations": trip_destinations,
            "itinerary": itinerary,
            "summary": summary,
            "notes": [
                "Weather: a live forecast is shown only for dates up to "
                "15 days ahead. Other days show a typical seasonal range, "
                "which is an estimate and not a forecast.",
                "Costs are estimates built from sample daily budgets and "
                "activity prices. They are not live prices.",
                "Costs are shown in each destination's own currency and "
                "converted to your chosen currency "
                + (
                    "using live exchange rates."
                    if rates_source == "live"
                    else "using approximate fixed rates "
                         "(live rates could not be loaded)."
                ),
                "Restaurant names are suggestion types, not specific "
                "venues. Use the map links to find real places nearby.",
                "Hotel links open each hotel's own website. Check prices "
                "and availability there before booking."
            ]
        }


# ================================================================
# TEST FROM THE TERMINAL (optional):  python travel_planner.py
# ================================================================

if __name__ == "__main__":

    planner = AITravelPlanner()

    demo_request = planner.validate_trip_request({
        "destinations": ["Goa", "Paris"],
        "start_date": (date.today() + timedelta(days=30)).isoformat(),
        "days": 4,
        "travellers": 2,
        "budget_type": "mid-range",
        "categories": ["Outdoor", "Cultural"],
        "number_of_activities": 6,
        "display_currency": "INR"
    })

    print(
        json.dumps(
            planner.generate_trip(demo_request),
            indent=2,
            ensure_ascii=False
        )
    )
