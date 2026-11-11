import unittest
from collector_03.adapter import decode


class InspectionExportTests(unittest.TestCase):
    def test_project_scoped_identity_is_preserved(self):
        rows = decode(b'<export><record project="P1" issue="H1"><kind>review</kind></record><record project="P2" issue="H1"><kind>inspection</kind></record></export>')
        self.assertEqual([(row["project"], row["issue"]) for row in rows], [("P1", "H1"), ("P2", "H1")])

    def test_attachment_names_are_not_read_by_parser(self):
        row = decode(b'<export><record><attachment>not-downloaded.txt</attachment></record></export>')[0]
        self.assertEqual(row["attachment"], "not-downloaded.txt")
