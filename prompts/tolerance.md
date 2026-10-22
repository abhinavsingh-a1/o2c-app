You read excerpts of a framework contract between Alpentech Industriebedarf GmbH and a customer.
Find the price tolerance: the maximum percentage by which an order price may deviate from the
agreed price without further approval.
Use ONLY the excerpts. If no tolerance is stated, use null.

Answer with a JSON object with exactly these two fields and nothing else:
- "tolerance_pct": the tolerance in percent as a number (2.5 for 2.5 %), or null.
- "evidence": the sentence you used, quoted from the excerpts (or why there is none).

Example:
{"tolerance_pct": 2.5, "evidence": "Price deviations of up to 2.5% are accepted."}
