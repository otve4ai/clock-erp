"""Daily 06:00 Moscow job; read-only against CDEK and ERP source data."""
import json
from app.clients.cdek import CdekError
from app.services.cdek_sync import CdekSync


def main():
    from app.web import CDEK_PAYOUTS, cdek_payout_references
    try:
        sync = CdekSync(CDEK_PAYOUTS)
        # Complete a normal cohort in the same daily invocation. Larger stages
        # remain resumable tomorrow (or through the manual refresh button).
        for _ in range(3):
            result = sync.run(cdek_payout_references)
            if result["errors"] or not result.get("remaining"):
                break
        print(json.dumps(result))
        return 1 if result["errors"] else 0
    except CdekError as error:
        print(json.dumps({"error": error.code, "message": str(error)}))
        return 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
