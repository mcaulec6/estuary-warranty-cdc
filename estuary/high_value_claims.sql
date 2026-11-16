-- Runs once for every change document from the warranty_claims collection
-- (inserts and updates alike). $field references a field of the incoming document.
--
-- Keeps only claims worth EUR 2,000 or more, labels their severity, and pulls the
-- market out of the dealer code (e.g. 'IE-003' -> 'IE').
--
-- The output collection is keyed on claim_id, so when a claim's status changes
-- the new document replaces the old one downstream instead of duplicating it.

SELECT
    $claim_id,
    $vin,
    $model,
    $component,
    $dealer_code,
    substr($dealer_code, 1, instr($dealer_code, '-') - 1) AS market,
    $claim_amount,
    CASE WHEN $claim_amount >= 5000 THEN 'critical' ELSE 'high' END AS severity,
    $status,
    $submitted_at,
    $updated_at
WHERE $claim_amount >= 2000;
