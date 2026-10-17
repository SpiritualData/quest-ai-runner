"""List price per 1,000 real backend calls, by the backend name the bench reports. A backend missing here is
reported as unpriced and its cost recorded as 0.0 (the note says so), never guessed. Check the vendor page before
relying on a number; a free tier can make the real invoice lower."""
PER_1K_USD = {"gemini-grounding": 35.0}
