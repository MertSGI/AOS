# AOS Disposable Canary Roadmap

1. Repair discount calculation defect in src/discount.py: calculate_discounted_price should calculate price * (1.0 - discount_percent / 100.0). Run python test_discount.py and commit.
2. Repair tax calculation defect in src/tax.py: calculate_total_with_tax should calculate amount * (1.0 + tax_rate). Run python test_tax.py and commit.
3. Verify all tests pass and reach project complete.
