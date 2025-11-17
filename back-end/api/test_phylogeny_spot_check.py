#!/usr/bin/env python
"""
Ad hoc spot check script for phylogeny parsing and matching.
Run this to verify the parsing works correctly with example files.
"""
import os
import sys
import json
import pandas as pd

# Add the parent directory to the path so we can import
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.helpers.publish import (
    parse_newick_tip_labels,
    parse_nexus_tip_labels,
    match_tip_label_to_scientific_name,
    update_occurrence_dynamic_properties,
)

def spot_check_nexus_parsing():
    """Spot check NEXUS file parsing."""
    print("=" * 60)
    print("SPOT CHECK: NEXUS File Parsing")
    print("=" * 60)
    
    nexus_path = os.path.join(
        os.path.dirname(__file__),
        'templates', 'examples', 'gaynor_et_al_v3', 'above50_genes.nex'
    )
    
    if not os.path.exists(nexus_path):
        print(f"ERROR: File not found: {nexus_path}")
        return
    
    with open(nexus_path, 'r', encoding='utf-8') as f:
        content = f.read()
    
    tip_labels = parse_nexus_tip_labels(content)
    
    print(f"\n✓ Parsed {len(tip_labels)} tip labels from NEXUS file")
    print(f"\nFirst 10 tip labels:")
    for i, label in enumerate(tip_labels[:10], 1):
        print(f"  {i}. {label}")
    
    # Check specific expected labels
    expected_labels = [
        'Berneuxia_thibetica_01',
        'Berneuxia_thibetica_02',
        'Cyrilla_racemiflora',
        'Diapensia_himalaica_01',
        'Shortia_sinensis_P',
    ]
    
    print(f"\n✓ Checking for expected labels:")
    for expected in expected_labels:
        if expected in tip_labels:
            print(f"  ✓ Found: {expected}")
        else:
            print(f"  ✗ Missing: {expected}")
    
    return tip_labels

def spot_check_matching():
    """Spot check tip label to scientific name matching."""
    print("\n" + "=" * 60)
    print("SPOT CHECK: Tip Label to Scientific Name Matching")
    print("=" * 60)
    
    test_cases = [
        ("Berneuxia_thibetica_01", "Berneuxia thibetica", True),
        ("Berneuxia_thibetica_P", "Berneuxia thibetica", True),
        ("Diapensia_himalaica_02", "Diapensia himalaica", True),
        ("Shortia_sinensis_P", "Shortia sinensis", True),
        ("Cyrilla_racemiflora", "Cyrilla racemiflora", True),
        ("Berneuxia_thibetica_01", "Diapensia himalaica", False),
        ("Unknown_label", "Unknown species", False),
    ]
    
    print("\n✓ Testing matching function:")
    all_passed = True
    for tip_label, scientific_name, expected in test_cases:
        result = match_tip_label_to_scientific_name(tip_label, scientific_name)
        status = "✓" if result == expected else "✗"
        if result != expected:
            all_passed = False
        print(f"  {status} '{tip_label}' <-> '{scientific_name}': {result} (expected {expected})")
    
    if all_passed:
        print("\n✓ All matching tests passed!")
    else:
        print("\n✗ Some matching tests failed!")
    
    return all_passed

def spot_check_occurrence_update():
    """Spot check updating occurrence data with phylogeny info."""
    print("\n" + "=" * 60)
    print("SPOT CHECK: Occurrence DataFrame Update")
    print("=" * 60)
    
    # Read example occurrence file
    occurrence_path = os.path.join(
        os.path.dirname(__file__),
        'templates', 'examples', 'gaynor_et_al_v3', 'occurrence.csv'
    )
    
    if not os.path.exists(occurrence_path):
        print(f"ERROR: File not found: {occurrence_path}")
        return
    
    df = pd.read_csv(occurrence_path, sep='\t', dtype=str)
    
    # Read NEXUS file
    nexus_path = os.path.join(
        os.path.dirname(__file__),
        'templates', 'examples', 'gaynor_et_al_v3', 'above50_genes.nex'
    )
    
    with open(nexus_path, 'r', encoding='utf-8') as f:
        nexus_content = f.read()
    
    tip_labels = parse_nexus_tip_labels(nexus_content)
    tree_files = [('above50_genes.nex', tip_labels)]
    
    # Create test DataFrame
    test_df = df[['occurrenceID', 'scientificName']].copy()
    test_df['dynamicProperties'] = ''
    
    print(f"\n✓ Processing {len(test_df)} occurrence records...")
    
    result_df = update_occurrence_dynamic_properties(test_df, tree_files)
    
    # Check specific records
    check_records = [
        ('DBT01_clean', 'Berneuxia thibetica', 'Berneuxia_thibetica_01'),
        ('DBT03_clean', 'Berneuxia thibetica', 'Berneuxia_thibetica_02'),
        ('S7451102', 'Cyrilla racemiflora', 'Cyrilla_racemiflora'),
        ('DDH04_clean', 'Diapensia himalaica', 'Diapensia_himalaica_01'),
    ]
    
    print(f"\n✓ Checking specific records:")
    all_passed = True
    for occ_id, sci_name, expected_tip in check_records:
        row = result_df[result_df['occurrenceID'] == occ_id]
        if len(row) == 0:
            print(f"  ✗ Record {occ_id} not found")
            all_passed = False
            continue
        
        row = row.iloc[0]
        dp_str = row['dynamicProperties']
        
        if not dp_str or dp_str.strip() == '':
            print(f"  ✗ {occ_id}: No dynamicProperties found")
            all_passed = False
            continue
        
        try:
            dp = json.loads(dp_str)
            if 'phylogenies' not in dp or len(dp['phylogenies']) == 0:
                print(f"  ✗ {occ_id}: No phylogenies found")
                all_passed = False
                continue
            
            tip_labels_found = [p['phyloTreeTipLabel'] for p in dp['phylogenies']]
            if expected_tip in tip_labels_found:
                print(f"  ✓ {occ_id} ({sci_name}): Found {expected_tip}")
                print(f"      All matches: {', '.join(tip_labels_found)}")
            else:
                print(f"  ✗ {occ_id} ({sci_name}): Expected {expected_tip}, found {tip_labels_found}")
                all_passed = False
        except json.JSONDecodeError as e:
            print(f"  ✗ {occ_id}: Invalid JSON in dynamicProperties: {e}")
            all_passed = False
    
    # Count how many records got matches
    matched_count = 0
    for idx, row in result_df.iterrows():
        if row['dynamicProperties'] and row['dynamicProperties'].strip():
            try:
                dp = json.loads(row['dynamicProperties'])
                if 'phylogenies' in dp and len(dp['phylogenies']) > 0:
                    matched_count += 1
            except:
                pass
    
    print(f"\n✓ Summary: {matched_count} out of {len(result_df)} records matched to phylogeny")
    
    if all_passed:
        print("\n✓ All occurrence update checks passed!")
    else:
        print("\n✗ Some occurrence update checks failed!")
    
    return all_passed

if __name__ == '__main__':
    print("\n" + "=" * 60)
    print("PHYLOGENY PARSING SPOT CHECKS")
    print("=" * 60)
    
    try:
        tip_labels = spot_check_nexus_parsing()
        matching_ok = spot_check_matching()
        update_ok = spot_check_occurrence_update()
        
        print("\n" + "=" * 60)
        print("OVERALL RESULT")
        print("=" * 60)
        if matching_ok and update_ok:
            print("✓ All spot checks passed!")
            sys.exit(0)
        else:
            print("✗ Some spot checks failed!")
            sys.exit(1)
    except Exception as e:
        print(f"\n✗ Error during spot checks: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

