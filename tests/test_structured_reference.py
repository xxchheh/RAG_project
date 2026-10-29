import unittest
import os
from rag_service.knowledge_units import prose_units
from rag_service.documents import parse_document
from unittest.mock import patch


class StructuredReferenceTests(unittest.TestCase):
    def test_atomic_table_and_formula_quarantine(self):
        blocks = [{'id':'b1','kind':'description','text':'| 转速(r/min) | 幅值(mm/s) |\n| 1000 | 2.8 |','chunk_ready':True,'locator':'PDF物理页 152 / table'},
                  {'id':'b2','kind':'uncertain','text':'公式待核对','chunk_ready':True,'locator':'PDF物理页 60 / formula'}]
        result = prose_units(blocks, {'b2'})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][1], blocks[0]['text'])

    def test_reference_does_not_extract_diagnostic_rules(self):
        expected={'blocks': [], 'rules': [], 'document_type':'reference'}
        with patch.dict(os.environ, {'RAG_REFERENCE_ENGINE':'docling'}), patch('rag_service.structured_reference.parse',return_value=expected), patch('rag_service.structured_reference.available',return_value=True):
            result=parse_document(b'input', 'book.pdf', document_type='reference', artifact_root='/tmp/check')
        self.assertEqual(result['rules'], [])

if __name__=='__main__':unittest.main()
