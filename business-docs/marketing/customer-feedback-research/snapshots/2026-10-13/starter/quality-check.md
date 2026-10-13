# Starter feedback export: independent QA (2026-10-13)

Read-only check of:

- `/Users/grigorii/Documents/Teplo/Customer Feedback Research/2026-10-13/starter/starter-feedback-private.jsonl`
- `/Users/grigorii/Documents/Teplo/Customer Feedback Research/2026-10-13/starter/starter-manifest.json`

No API request was made and no raw review text, customer identifiers, contact details, or order identifiers are included here.

## Completeness and integrity

The JSONL parses as 10,535 records with no invalid lines. Every row has a non-null feedback `id`; all 10,535 IDs are unique. Manifest `unique_feedback_ids` and `rows` both equal 10,535. There are 64 gaps in the numeric ID sequence; that is not evidence of pagination loss by itself. Manifest page offsets are contiguous across 22 pages, with 35 records in the last page and `tail_rows=0`. `total_before` and `total_after` both report 10,535 and average rating 4.56. The SHA-256 of the private JSONL matches the manifest.

Record dates span `2023-03-02T11:20:08.893Z` through `2026-10-13T14:54:34.231Z`. This is a multi-year archive, not a current-period sample.

## Ratings and available text

The rating distribution is 1★ 572, 2★ 264, 3★ 491, 4★ 559, 5★ 8,649. Recomputed weighted average is 4.5614, consistent with 4.56 in the manifest. There are 3,814 non-empty comments (36.2%) and 6,721 empty-string comments (63.8%). Tags and answers are empty for all 10,535 rows; all status values are `accepted`. These are workflow/data fields: empty tags mean no native tag coding was returned, and `accepted` does not indicate sentiment. No thematic coding was performed in this QA.

## Order and shop linkage

Every feedback row contains an `order` object with a unique `order.id` and `order.starterId`; 10,512 of 10,535 have `order.posId`. This supports a Starter-feedback-to-Starter-order reference, but does not by itself prove a cross-system match to iiko or a Revvy conversation.

`order.shop` has only `city` and `street`, with no shop ID. Its two locations account for 9,265 records at Черникова, 4В and 1,270 at Гагарина, 10. The manifest's filtered totals report `shops=3`: 9,265 and `shops=105` (test): 0. Those filtered totals sum to 9,265, leaving 1,270 records without a verified `shop_id` mapping. The location text points to Гагарина, 10, but do not assign those rows to shop 105 without an ID-bearing relation.

## How to use the export

Use the full set for dated rating trends and the 3,814 text-bearing rows for qualitative review analysis. Keep the denominator explicit: text-bearing comments are a selected subset of feedback, while the 6,721 star-only records cannot explain a guest's rating. Since this is post-order feedback, it describes responding purchasers; it cannot establish why non-buyers chose another service or did not order. Use themes as hypotheses for the growth research, then test purchase behavior and full contribution economics separately. Keep the source outside Git: the JSONL includes customer and order identifiers, and free text can contain identifying information.

## Limited Revvy overlap screen

I checked dates and issue wording for three candidate pairs against the 12 coded specific internal Revvy episodes. These are only possible overlaps; the redacted internal coding has no common order ID for matching, and similar complaint words are not proof of the same guest or order.

- Revvy E02 (2026-10-04) ↔ one Starter comment dated 2026-10-09: broad overlap on delay and cold/temperature wording; five-day gap. Low-confidence candidate.
- Revvy E06 (2026-10-04) ↔ one Starter comment dated 2026-10-03: the candidate mentions wasabi, but no clear dissatisfaction marker was detected. Weak candidate; likely insufficient evidence.
- Revvy E06 (2026-10-04) ↔ one Starter comment dated 2026-10-11: add-on/sauce wording and a negative marker occur in the same comment; seven-day gap and generic terms make this weak.

Do not deduplicate or count these as same-order incidents without an order-ID join confirmed across systems. No full-dialogue or thematic review was done for this screen.
