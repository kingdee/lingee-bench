"""Oracle computes from inputs; it never imports verifier expectations."""

import json
import sys
from pathlib import Path


def solve(workspace: Path) -> dict:
    def read(name):
        return json.loads((workspace / "data" / name).read_text())

    orders = {p["po_id"]: p for p in read("purchase_orders.json")}
    receipts = read("receipts.json")
    seen = set()
    decisions = []
    for invoice in sorted(read("invoices.json"), key=lambda i: i["invoice_id"]):
        po = orders.get(invoice["po_id"])
        evidence = [r for r in receipts if po and r["po_id"] == po["po_id"] and r["status"] == "accepted"]
        amount = invoice["quantity"] * invoice["unit_price_cents"]
        key = (invoice["supplier_id"], invoice["invoice_number"])
        decision = "blocked"
        if key in seen:
            reason = "duplicate_invoice"
        elif po is None:
            reason = "missing_po"
        elif invoice["supplier_id"] != po["supplier_id"]:
            reason = "supplier_mismatch"
        elif po["approval_status"] != "approved":
            reason = "po_not_approved"
        elif invoice["quantity"] > min(po["quantity"], sum(r["quantity"] for r in evidence)):
            reason = "quantity_exceeds_received"
        elif invoice["unit_price_cents"] != po["unit_price_cents"]:
            reason = "price_mismatch"
        elif amount > 1000000:
            decision, reason = "needs_approval", "amount_requires_approval"
        else:
            decision, reason = "ready_for_review", "matched"
        seen.add(key)
        decisions.append(dict(invoice_id=invoice["invoice_id"], po_id=invoice["po_id"],
                              receipt_ids=sorted(r["receipt_id"] for r in evidence),
                              amount_cents=amount, decision=decision, reason_code=reason))
    ready = [d for d in decisions if d["decision"] == "ready_for_review"]
    approval = [d for d in decisions if d["decision"] == "needs_approval"]
    return dict(batch_id=read("state.json")["batch_id"], decisions=decisions, payments=[],
                summary=dict(invoice_count=len(decisions), ready_count=len(ready),
                             approval_count=len(approval), blocked_count=len(decisions)-len(ready)-len(approval),
                             ready_amount_cents=sum(d["amount_cents"] for d in ready),
                             approval_amount_cents=sum(d["amount_cents"] for d in approval)))


if __name__ == "__main__":
    workspace = Path(sys.argv[1])
    (workspace / "output").mkdir(exist_ok=True)
    (workspace / "output/reconciliation.json").write_text(json.dumps(solve(workspace), indent=2) + "\n")
