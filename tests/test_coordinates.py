import unittest
from pyLatLong2ParcelMap import parse_coord

class CoordinateTests(unittest.TestCase):
    def test_decimal(self):
        self.assertEqual(parse_coord('-95.5'), -95.5)
    def test_dms(self):
        self.assertAlmostEqual(parse_coord('95d30m0sW'), -95.5)
    def test_conflict(self):
        with self.assertRaises(ValueError):
            parse_coord('-95d30mE')
    def test_invalid_minutes(self):
        with self.assertRaises(ValueError):
            parse_coord('95d61mW')

if __name__ == '__main__':
    unittest.main()
