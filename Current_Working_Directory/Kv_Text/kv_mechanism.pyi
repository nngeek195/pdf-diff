# kv_mechanism.pyi
# This is a stub file to help VS Code understand our compiled C++ library.

from typing import List, Dict, Any

def run_diff(old_dicts: List[Dict[str, Any]], new_dicts: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """
    Run the Kv spatial diffing engine.
    
    Returns a dictionary containing:
    - 'removed_words': List of words deleted from the old PDF.
    - 'added_words': List of words added to the new PDF.
    """
    ...