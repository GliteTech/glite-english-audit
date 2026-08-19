"""Fixed website destination for the browser's report form handoff."""

# The product's own domain, not the deployment's. This was pinned to
# `glite-english-audit-website-eta.vercel.app` -- Vercel's auto-generated alias
# for the project -- which was the only address the website answered on when
# this handoff was specified. Both resolve to the same deployment today, so a
# local audit was finishing on an unbranded URL for no reason, and the CSP
# `form-action` built from this constant was pinned to a hostname the project
# does not control the naming of.
REPORT_PAGE_ORIGIN = "https://gliteaudit.com"
REPORT_PAGE_URL = f"{REPORT_PAGE_ORIGIN}/report"
