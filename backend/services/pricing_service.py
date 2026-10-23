"""
Stage 1: Historical lookup

Step-by-step
1️⃣ Extract keywords from the current prompt.
2️⃣ Build a Mongo query using ONLY those keywords
3️⃣ Fetch all matching screens from the database
4️⃣ Score each match based on keyword similarity

5️⃣ Filter out low-scoring matches
if score >= MIN_KEYWORD_MATCH_SCORE:
    keep it
else:
    discard it

5️⃣ Minimum match requirement
if len(scored_matches) < 3:
    return None

6️⃣ Cost calculation (ONLY from similar screens)

If history survives:
Take top ~20 matches
Extract total_cost_usd
Sort
Take median
Apply:
safety buffer (1.2×)
markup (5×)
convert USD → credits
👉 Only those similar screens affect the estimate.

Stage 2: Complexity heuristic (fallback)
If any part of Stage 1 fails, this runs.

Step-by-step
1️⃣ Analyze the current prompt
2️⃣ Extract keywords and patterns
3️⃣ Score based on complexity indicators
4️⃣ Determine complexity level
5️⃣ Assign credits based on score and word count
6️⃣ Return the estimate
"""
import logging
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from services.database import Database
from services.screen_cost_service import SCREEN_COST_COLLECTION

# Configure logger
logger = logging.getLogger(__name__)


# --- Configuration constants (can be externalized later) ---

# Credit economics (Phase 2)
# NOTE: These values are calibrated from real historical stats:
# - Median cost ≈ $0.206 (p50)
# - p25 ≈ $0.097, p75 ≈ $0.269, p90 ≈ $0.351, p95 ≈ $0.636
CREDIT_VALUE_USD = 0.001  # 1 credit = $0.001 (1k credits = $1)
MARKUP_MULTIPLIER = 5.0   # 5x markup over raw LLM cost

# Heuristic credit bands (derived from percentiles above, with 5x markup):
# - Simple   ≈ p25  → ~500 credits
# - Typical  ≈ p50  → ~1000–1200 credits
# - Complex  ≈ p75  → ~1500 credits
# - Very high≈ p90+ → ~1800–2000+ credits
SIMPLE_SCREEN_CREDITS = 500
STANDARD_SCREEN_CREDITS = 1100
COMPLEX_SCREEN_CREDITS = 1500
VERY_COMPLEX_SCREEN_CREDITS = 2000

# Historical estimation parameters
MIN_HISTORICAL_MATCHES = 3
HISTORICAL_LOOKUP_LIMIT = 50
# Slightly conservative buffer on top of real median costs
SAFETY_BUFFER_MULTIPLIER = 1.2
MIN_KEYWORD_MATCH_SCORE = 0.10  # Minimum 10% keyword overlap


@dataclass
class CostEstimate:
    estimated_credits: int
    complexity: str
    confidence: str
    source: str  # "complexity_heuristic" or "historical_lookup"
    raw_cost_usd: Optional[float] = None
    matched_screens: Optional[int] = None
    keyword_match_score: Optional[float] = None


# Enhanced stop words list
_STOP_WORDS = {
    "the", "and", "for", "with", "this", "that", "from", "your", "have",
    "screen", "page", "want", "need", "create", "a", "an",
    "make", "build", "show", "display", "view", "would", "like", "please",
    "can", "you", "will", "should", "could", "able", "using", "used",
}
_IMPORTANT_SHORT_WORDS = {"api", "ocr", "csv", "pdf", "auth", "dxf", "ui", "llm",
    "docx", "doc", "xlsx", "xls", "ppt", "pptx", "txt", "json", "xml", "html", "css", 
    "js", "ts", "py", "java", "c", "cpp", "h", "hpp", "c++"}

def _extract_keywords(text: str, max_keywords: int = 10) -> List[str]:
    """
    Enhanced keyword extractor with better filtering and scoring.
    
    Args:
        text: Input text to extract keywords from
        max_keywords: Maximum number of keywords to return
        
    Returns:
        List of keywords sorted by relevance
    """
    if not text:
        logger.debug("Empty text provided for keyword extraction")
        return []
    
    text_lower = text.lower()
    
    # Extract words (3+ chars for better signal)
    words = re.findall(r"[a-zA-Z]{3,}", text_lower)
    
    if not words:
        logger.debug("No valid words found in text")
        return []
    
    # Calculate word frequencies and scores
    freq: Dict[str, int] = {}
    positions: Dict[str, List[int]] = {}  # Track positions for relevance
    
    for idx, word in enumerate(words):
        if word in _STOP_WORDS and word not in _IMPORTANT_SHORT_WORDS:
            continue
            
        freq[word] = freq.get(word, 0) + 1
        if word not in positions:
            positions[word] = []
        positions[word].append(idx)
    
    if not freq:
        logger.debug("All words filtered out as stop words")
        return []
    
    # Score words based on:
    # 1. Frequency (more important)
    # 2. Position (earlier is slightly better)
    # 3. Length (longer words often more specific)
    scored_words: List[Tuple[str, float]] = []
    
    for word, count in freq.items():
        # Frequency score (normalized)
        freq_score = min(count / 3.0, 1.0)  # Cap at 3 mentions
        
        # Position score (first occurrence matters)
        first_pos = min(positions[word])
        pos_score = 1.0 - (first_pos / max(len(words), 1)) * 0.3  # Max 30% penalty
        
        # Length score (favor longer, more specific terms)
        length_score = min(len(word) / 12.0, 1.0)  # Normalize to ~12 chars
        
        # Combined score
        total_score = (freq_score * 0.6) + (pos_score * 0.2) + (length_score * 0.2)
        scored_words.append((word, total_score))
    
    # Sort by score (descending) and take top N
    scored_words.sort(key=lambda x: (-x[1], x[0]))
    keywords = [word for word, _ in scored_words[:max_keywords]]
    
    logger.debug(
        f"Extracted {len(keywords)} keywords from {len(words)} words: {keywords}"
    )
    
    return keywords


def _calculate_keyword_match_score(
    prompt_keywords: List[str],
    historical_prompt: str
) -> float:
    """
    Calculate similarity score between prompt keywords and historical prompt.
    
    Returns:
        Score between 0 and 1 indicating similarity
    """
    if not prompt_keywords or not historical_prompt:
        return 0.0
    
    historical_text = historical_prompt.lower()
    matched = sum(1 for kw in prompt_keywords if kw in historical_text)
    
    return matched / len(prompt_keywords)


def _complexity_from_prompt(prompt: str) -> CostEstimate:
    """
    Enhanced complexity analyzer with better pattern detection.
    Implements Phase 3.1 with improved heuristics.
    """
    if not prompt:
        logger.warning("Empty prompt provided to complexity analyzer")
        return CostEstimate(
            estimated_credits=STANDARD_SCREEN_CREDITS,
            complexity="medium",
            confidence="low",
            source="complexity_heuristic",
        )

    word_count = len(re.findall(r"\w+", prompt))
    text_lower = prompt.lower()
    
    logger.debug(f"Analyzing prompt complexity: {word_count} words")

    # Enhanced keyword patterns with weights
    complexity_indicators = {
        "very_high": {
            "keywords": [
                "workflow", "multiple entities", "admin panel", "role-based",
                "permissions", "multi-tenant", "real-time", "websocket",
                "complex dashboard", "analytics", "reporting engine", "external data", "extraction",
            ],
            "weight": 3
        },
        "high": {
            "keywords": [
                "dashboard", "crud", "multi-step", "authentication", "authorization",
                "data table", "chart", "graph", "filtering", "pagination", "data extraction", "ocr", 
                "search functionality", "file upload", "api integration", "external data"
            ],
            "weight": 2
        },
        "medium": {
            "keywords": [
                "form with validation", "table", "list", "modal", "dropdown",
                "tabs", "accordion", "navigation", "responsive"
            ],
            "weight": 1
        },
        "low": {
            "keywords": [
                "simple form", "basic", "single page", "static", "read-only",
                "display only", "simple list"
            ],
            "weight": 0
        }
    }
    
    # Calculate complexity score
    complexity_score = 0
    matched_indicators = []
    
    for level, config in complexity_indicators.items():
        for keyword in config["keywords"]:
            if keyword in text_lower:
                complexity_score += config["weight"]
                matched_indicators.append((level, keyword))
    
    logger.debug(
        f"Complexity score: {complexity_score}, "
        f"matched indicators: {matched_indicators}"
    )
    
    # Determine complexity based on score and word count
    if complexity_score >= 6 or word_count > 250:
        complexity = "very_high"
        credits = VERY_COMPLEX_SCREEN_CREDITS
        confidence = "high" if complexity_score >= 9 else "medium"
    elif complexity_score >= 4 or word_count > 150:
        complexity = "high"
        credits = COMPLEX_SCREEN_CREDITS
        confidence = "high" if complexity_score >= 6 else "medium"
    elif complexity_score >= 2 or (80 <= word_count <= 150):
        complexity = "medium"
        credits = STANDARD_SCREEN_CREDITS
        confidence = "medium"
    else:
        complexity = "low"
        credits = SIMPLE_SCREEN_CREDITS
        confidence = "medium" if word_count < 50 else "low"
    
    logger.info(
        f"Complexity heuristic result: {complexity} complexity, "
        f"{credits} credits, {confidence} confidence"
    )
    
    return CostEstimate(
        estimated_credits=credits,
        complexity=complexity,
        confidence=confidence,
        source="complexity_heuristic",
    )


async def _estimate_from_history(
    project_id: Optional[str],
    prompt: str,
    min_matches: int = MIN_HISTORICAL_MATCHES,
) -> Optional[CostEstimate]:
    """
    Enhanced historical estimator with better matching and logging.
    Implements Phase 3.2 with similarity scoring.
    """
    logger.info(
        f"Attempting historical lookup for project_id={project_id}, "
        f"min_matches={min_matches} required."
    )
    
    keywords = _extract_keywords(prompt)
    if not keywords:
        logger.warning("No keywords extracted from prompt for historical lookup")
        return None

    logger.debug(f"Using keywords for historical search: {keywords}")

    try:
        coll = Database.get_collection(SCREEN_COST_COLLECTION)

        # Build regex query for all keywords
        or_clauses: List[Dict[str, Any]] = [
            {"user_prompt": {"$regex": kw, "$options": "i"}} for kw in keywords
        ]

        query: Dict[str, Any] = {
            "$or": or_clauses,
            "total_cost_usd": {"$exists": True, "$ne": None, "$gt": 0}
        }
        
        if project_id:
            query["project_id"] = project_id
            logger.debug(f"Filtering by project_id: {project_id}")

        # Fetch potential matches
        cursor = coll.find(
            query,
            projection={"total_cost_usd": 1, "user_prompt": 1}
        ).limit(HISTORICAL_LOOKUP_LIMIT)
        
        matches: List[Dict[str, Any]] = await cursor.to_list(
            length=HISTORICAL_LOOKUP_LIMIT
        )
        
        logger.debug(f"Found {len(matches)} potential historical matches")

        if len(matches) < min_matches:
            logger.info(
                f"Insufficient historical matches: {len(matches)} < {min_matches}"
            )
            return None

        # Score and filter matches by keyword similarity
        scored_matches: List[Tuple[Dict[str, Any], float]] = []
        
        for match in matches:
            if "user_prompt" not in match:
                continue
                
            score = _calculate_keyword_match_score(
                keywords,
                match["user_prompt"]
            )
            
            if score >= MIN_KEYWORD_MATCH_SCORE:
                scored_matches.append((match, score))
        
        logger.debug(
            f"After similarity filtering: {len(scored_matches)} matches "
            f"(threshold: {MIN_KEYWORD_MATCH_SCORE})"
        )
        
        if len(scored_matches) < min_matches:
            logger.info(
                f"Insufficient similar matches after filtering: "
                f"{len(scored_matches)} < {min_matches}"
            )
            return None
        
        # Sort by similarity score and extract costs
        scored_matches.sort(key=lambda x: -x[1])
        
        # Use top matches for cost calculation
        top_matches = scored_matches[:min(20, len(scored_matches))]
        costs = [
            float(m[0].get("total_cost_usd", 0.0))
            for m in top_matches
            if m[0].get("total_cost_usd") is not None
        ]
        
        if not costs:
            logger.warning("No valid costs found in matched records")
            return None
        
        costs.sort()
        
        # Use median for robustness
        median_cost = costs[len(costs) // 2]
        avg_match_score = sum(score for _, score in top_matches) / len(top_matches)
        
        logger.info(
            f"Historical cost calculation: median=${median_cost:.4f} "
            f"from {len(top_matches)} matches, "
            f"avg similarity={avg_match_score:.2f}"
        )
        
        # Add safety buffer, apply markup, convert to credits
        buffered_cost = median_cost * SAFETY_BUFFER_MULTIPLIER
        marked_up_cost = buffered_cost * MARKUP_MULTIPLIER
        estimated_credits = int(math.ceil(marked_up_cost / CREDIT_VALUE_USD))
        
        # Determine confidence based on match quality
        if len(top_matches) >= 10 and avg_match_score >= 0.4:
            confidence = "high"
        elif len(top_matches) >= 5 and avg_match_score >= 0.25:
            confidence = "medium"
        else:
            confidence = "low"
        
        logger.info(
            f"Historical estimate: {estimated_credits} credits "
            f"(raw=${median_cost:.4f}, buffered=${buffered_cost:.4f}, "
            f"marked_up=${marked_up_cost:.4f}), confidence={confidence}"
        )
        
        return CostEstimate(
            estimated_credits=estimated_credits,
            complexity="historical",
            confidence=confidence,
            source="historical_lookup",
            raw_cost_usd=median_cost,
            matched_screens=len(top_matches),
            keyword_match_score=avg_match_score,
        )
        
    except Exception as e:
        logger.error(f"Error during historical lookup: {str(e)}", exc_info=True)
        return None


async def estimate_screen_cost(
    user_prompt: str,
    project_id: Optional[str] = None,
) -> CostEstimate:
    """
    Public API for screen cost estimation with comprehensive logging.

    Strategy:
    1. Try historical lookup (Phase 3.2) - most accurate when data available
    2. Fallback to complexity analyzer (Phase 3.1) - rule-based heuristics
    
    Args:
        user_prompt: The user's screen generation request
        project_id: Optional project ID for project-specific history
        
    Returns:
        CostEstimate with credits, complexity, confidence, and metadata
    """
    logger.info(
        f"Starting cost estimation for project_id={project_id}, "
        f"prompt_length={len(user_prompt)} chars"
    )
    
    if not user_prompt or not user_prompt.strip():
        logger.warning("Empty or whitespace-only prompt provided")
        return CostEstimate(
            estimated_credits=STANDARD_SCREEN_CREDITS,
            complexity="medium",
            confidence="low",
            source="default_fallback",
        )
    
    # Strategy 1: Historical lookup (preferred)
    try:
        historical = await _estimate_from_history(
            project_id=project_id,
            prompt=user_prompt
        )
        
        if historical:
            logger.info(
                f"Using historical estimate: {historical.estimated_credits} credits "
                f"from {historical.matched_screens} similar screens, "
                f"confidence={historical.confidence}"
            )
            return historical
        else:
            logger.info("Historical lookup returned no matches, using fallback")
            
    except Exception as e:
        logger.error(
            f"Historical lookup failed: {str(e)}, falling back to heuristic",
            exc_info=True
        )
    
    # Strategy 2: Complexity heuristic (fallback)
    logger.info("Using complexity heuristic for estimation")
    heuristic_result = _complexity_from_prompt(user_prompt)
    
    logger.info(
        f"Final estimate: {heuristic_result.estimated_credits} credits, "
        f"complexity={heuristic_result.complexity}, "
        f"confidence={heuristic_result.confidence}, "
        f"source={heuristic_result.source}"
    )
    
    return heuristic_result