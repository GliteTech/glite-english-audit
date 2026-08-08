"""Token and time estimation from the committed calibration profile.

The profile in ``calibration/token-usage-profile.json`` holds only numerical
coefficients per step, runtime, model, and effort (specification, 10.1, 3.7).
Estimators in this package are pure functions over those coefficients plus the
private cross-run calibration history (specification, 10.2, 10.3).
"""
