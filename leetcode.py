"""LeetCode public GraphQL API client."""
import re
import httpx
from typing import Optional

LEETCODE_GRAPHQL = "https://leetcode.com/graphql/"


def normalize_leetcode_username(raw: str) -> str:
    """Turn any LeetCode URL / handle into a clean username.

    Accepts:
      - 'nSKWHoKvyX'
      - 'u/nSKWHoKvyX'
      - '@nSKWHoKvyX'
      - 'https://leetcode.com/u/nSKWHoKvyX/'
      - 'https://leetcode.com/nSKWHoKvyX'
      - 'leetcode.com/u/nSKWHoKvyX/'
    Returns: 'nSKWHoKvyX'
    """
    if not raw:
        return ""
    s = raw.strip()
    # Strip protocol/host if a full URL was pasted
    m = re.search(r"leetcode\.com/(?:u/)?([^/?#\s]+)", s, flags=re.IGNORECASE)
    if m:
        return m.group(1).strip("/")
    # Strip 'u/' or '@' prefix if just those were pasted
    if s.lower().startswith("u/"):
        s = s[2:]
    if s.startswith("@"):
        s = s[1:]
    return s.strip("/").strip()

USER_QUERY = """
query userCompleteProfile($username: String!) {
  matchedUser(username: $username) {
    username
    profile {
      ranking
      realName
      userAvatar
      countryName
      reputation
    }
    submitStatsGlobal {
      acSubmissionNum { difficulty count }
    }
    userCalendar {
      streak
      totalActiveDays
      submissionCalendar
    }
    badges { id displayName icon }
  }
  userContestRanking(username: $username) {
    attendedContestsCount
    rating
    globalRanking
    topPercentage
  }
}
"""


async def fetch_leetcode_stats(username: str) -> Optional[dict]:
    """Fetch public LeetCode stats. Returns dict or None if user not found."""
    username = normalize_leetcode_username(username or "")
    if not username:
        return None
    headers = {
        "Content-Type": "application/json",
        "Referer": f"https://leetcode.com/{username}/",
        "User-Agent": "Mozilla/5.0 (compatible; LeetCodeTracker/1.0)",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                LEETCODE_GRAPHQL,
                json={"query": USER_QUERY, "variables": {"username": username}},
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json().get("data") or {}
    except Exception as e:
        return {"__error__": str(e)}

    matched = data.get("matchedUser")
    if not matched:
        return None

    stats = {"easy": 0, "medium": 0, "hard": 0, "total": 0}
    for item in (matched.get("submitStatsGlobal") or {}).get("acSubmissionNum", []):
        diff = (item.get("difficulty") or "").lower()
        c = item.get("count") or 0
        if diff in ("easy", "medium", "hard"):
            stats[diff] = c
        elif diff == "all":
            stats["total"] = c
    if not stats["total"]:
        stats["total"] = stats["easy"] + stats["medium"] + stats["hard"]

    profile = matched.get("profile") or {}
    calendar = matched.get("userCalendar") or {}
    contest = data.get("userContestRanking") or {}

    return {
        "username": matched.get("username"),
        "avatar": profile.get("userAvatar"),
        "real_name": profile.get("realName"),
        "country": profile.get("countryName"),
        "reputation": profile.get("reputation"),
        "ranking": profile.get("ranking"),
        "easy": stats["easy"],
        "medium": stats["medium"],
        "hard": stats["hard"],
        "total_solved": stats["total"],
        "streak": calendar.get("streak"),
        "total_active_days": calendar.get("totalActiveDays"),
        "submission_calendar": calendar.get("submissionCalendar"),
        "contest_rating": contest.get("rating"),
        "contest_attended": contest.get("attendedContestsCount"),
        "contest_global_ranking": contest.get("globalRanking"),
        "contest_top_percentage": contest.get("topPercentage"),
        "badges_count": len(matched.get("badges") or []),
    }
