#!/usr/bin/env python3
"""
Test & estimate token savings from bootstrap optimizations (Stage 1 + Stage 2).

Simulates different corpus sizes and measures char/token reduction.
Token estimation: ~1 token ≈ 4 chars (Claude's rough tokenization).
"""
from pathlib import Path
import sys
from typing import List, Dict, Any

# Add the package to path
sys.path.insert(0, str(Path(__file__).parent))

from quest_ai_runner.adapters.file_context_store import (
    _select_representative_files,
    _extract_terms,
    _extract_file_snippet,
    _summarize_snippet,
)


def generate_mock_files(num_files: int, num_folders: int = 5) -> List[str]:
    """Generate mock file paths with realistic distribution."""
    files = []
    files_per_folder = num_files // num_folders
    for fi in range(num_folders):
        folder = f"folder{fi}"
        for fj in range(files_per_folder):
            fname = f"module_{fj}"
            ext = [".py", ".ts", ".js", ".go", ".rs"][fj % 5]
            files.append(f"{folder}/{fname}{ext}")
    return files[:num_files]


def estimate_tokens(text: str) -> int:
    """Rough token estimate: 1 token ≈ 4 chars."""
    return len(text) // 4


def measure_stage1_output(file_paths: List[str]) -> Dict[str, Any]:
    """Measure Stage 1 output: full vs sampled file list."""
    # Full output (all file paths)
    full_output = "\n".join(file_paths)
    full_chars = len(full_output)
    full_tokens = estimate_tokens(full_output)

    # Sampled output (with TF-DF-IDF)
    sampled = _select_representative_files(file_paths, samples_per_folder=3)
    sampled_output = "\n".join(sampled)
    sampled_chars = len(sampled_output)
    sampled_tokens = estimate_tokens(sampled_output)

    reduction_pct = 100 * (1 - sampled_chars / full_chars) if full_chars > 0 else 0

    return {
        "full_files": len(file_paths),
        "sampled_files": len(sampled),
        "full_chars": full_chars,
        "full_tokens": full_tokens,
        "sampled_chars": sampled_chars,
        "sampled_tokens": sampled_tokens,
        "reduction_pct": reduction_pct,
    }


def measure_stage2_output(area_files: List[str]) -> Dict[str, Any]:
    """Measure Stage 2 output: paths only vs sampled + snippets + summaries.

    Note: _extract_file_snippet requires actual files to exist, so we simulate
    with mock snippets instead.
    """
    # Baseline: just file paths (Stage 2 without optimization)
    paths_output = "\n".join(area_files)
    paths_chars = len(paths_output)
    paths_tokens = estimate_tokens(paths_output)

    # With optimization: sample + mock snippets + summaries
    sampled_files = _select_representative_files(area_files, samples_per_folder=2) if len(area_files) > 5 else area_files

    # Simulate file snippets (mock)
    file_entries = []
    for fpath in sampled_files:
        # Mock snippet based on filename
        if "test" in fpath:
            snippet = f"# Test file for {fpath}\ndef test_main():\n    assert True"
        elif "middleware" in fpath:
            snippet = f'"""Middleware layer for {fpath}."""\ndef process(req): return req'
        else:
            snippet = f'"""Module {fpath}"""\ndef main(): pass'

        summarized = _summarize_snippet(fpath, snippet)
        file_entries.append(summarized)

    optimized_output = "\n---\n".join(file_entries)
    optimized_chars = len(optimized_output)
    optimized_tokens = estimate_tokens(optimized_output)

    reduction_pct = 100 * (1 - optimized_chars / paths_chars) if paths_chars > 0 else 0

    return {
        "area_files": len(area_files),
        "sampled_files": len(sampled_files),
        "paths_chars": paths_chars,
        "paths_tokens": paths_tokens,
        "optimized_chars": optimized_chars,
        "optimized_tokens": optimized_tokens,
        "reduction_pct": reduction_pct,
    }


def main():
    """Run tests across different corpus sizes."""
    print("=" * 80)
    print("BOOTSTRAP TOKEN SAVINGS ANALYSIS")
    print("=" * 80)

    corpus_sizes = [100, 250, 500, 1000, 2500, 5000]
    num_folders_per_size = {100: 5, 250: 8, 500: 10, 1000: 15, 2500: 25, 5000: 40}

    print("\n" + "=" * 80)
    print("STAGE 1: Representative File Sampling (TF-DF-IDF)")
    print("=" * 80)
    print(f"{'Corpus Size':<15} {'Full Files':<12} {'Sampled':<10} {'Chars Saved':<15} {'Tokens Saved':<15} {'Reduction':<12}")
    print("-" * 80)

    stage1_results = []
    for size in corpus_sizes:
        num_folders = num_folders_per_size[size]
        files = generate_mock_files(size, num_folders=num_folders)
        result = measure_stage1_output(files)
        stage1_results.append(result)

        print(
            f"{size:<15} {result['full_files']:<12} {result['sampled_files']:<10} "
            f"{result['full_chars'] - result['sampled_chars']:<15} "
            f"{result['full_tokens'] - result['sampled_tokens']:<15} "
            f"{result['reduction_pct']:.1f}%"
        )

    print("\n" + "=" * 80)
    print("STAGE 2: File Sampling + Snippet Extraction + Length-Aware Summary")
    print("=" * 80)
    print(f"{'Area Size':<15} {'Files':<10} {'Sampled':<10} {'Chars Saved':<15} {'Tokens Saved':<15} {'Reduction':<12}")
    print("-" * 80)

    # Test Stage 2 with various area sizes (simulate areas from Stage 1)
    area_sizes = [10, 50, 100, 250, 500, 1000]
    for area_size in area_sizes:
        files = generate_mock_files(area_size, num_folders=max(2, area_size // 100))
        result = measure_stage2_output(files)

        print(
            f"{area_size:<15} {result['area_files']:<10} {result['sampled_files']:<10} "
            f"{result['paths_chars'] - result['optimized_chars']:<15} "
            f"{result['paths_tokens'] - result['optimized_tokens']:<15} "
            f"{result['reduction_pct']:.1f}%"
        )

    print("\n" + "=" * 80)
    print("COMBINED ESTIMATE: Stage 1 + Stage 2 Savings")
    print("=" * 80)
    print("Assuming a typical bootstrap with 1000 files across ~100 areas:")
    print()

    # Simulation: 1000 files → ~100 areas (10 files per area on average)
    files_1000 = generate_mock_files(1000, num_folders=15)
    stage1 = measure_stage1_output(files_1000)
    print(f"Stage 1 optimization:")
    print(f"  Full listing:    {stage1['full_tokens']:,} tokens")
    print(f"  With sampling:   {stage1['sampled_tokens']:,} tokens")
    print(f"  Savings:         {stage1['full_tokens'] - stage1['sampled_tokens']:,} tokens (~{stage1['reduction_pct']:.0f}%)")
    print()

    # For each area in Stage 1 output, estimate Stage 2 savings
    # Rough: if 100 areas with ~10 files each
    avg_area_size = 10
    mock_area = files_1000[:avg_area_size]
    stage2 = measure_stage2_output(mock_area)
    print(f"Stage 2 optimization (per area, avg {avg_area_size} files):")
    print(f"  Paths only:      {stage2['paths_tokens']:,} tokens")
    print(f"  With snippets:   {stage2['optimized_tokens']:,} tokens")
    print(f"  Savings:         {stage2['paths_tokens'] - stage2['optimized_tokens']:,} tokens (~{stage2['reduction_pct']:.0f}%)")
    print()

    # Estimate total for 100 areas
    num_areas = 100
    combined_stage2_savings = num_areas * (stage2['paths_tokens'] - stage2['optimized_tokens'])
    total_stage1_tokens = stage1['full_tokens']
    total_stage2_without_opt = num_areas * stage2['paths_tokens']
    total_with_opt = stage1['sampled_tokens'] + (num_areas * stage2['optimized_tokens'])

    print(f"Combined (1000 files, ~{num_areas} areas):")
    print(f"  Stage 1 (all paths):          {total_stage1_tokens:,} tokens")
    print(f"  Stage 2 (all areas, no opt):  +{total_stage2_without_opt:,} tokens")
    print(f"  Total without optimization:   {total_stage1_tokens + total_stage2_without_opt:,} tokens")
    print()
    print(f"  Stage 1 (sampled):            {stage1['sampled_tokens']:,} tokens")
    print(f"  Stage 2 (sampled+snippets):   +{num_areas * stage2['optimized_tokens']:,} tokens")
    print(f"  Total with optimization:      {total_with_opt:,} tokens")
    print()
    total_savings = (total_stage1_tokens + total_stage2_without_opt) - total_with_opt
    overall_reduction = 100 * total_savings / (total_stage1_tokens + total_stage2_without_opt)
    print(f"  TOTAL SAVINGS:                {total_savings:,} tokens (~{overall_reduction:.0f}%)")
    print()


if __name__ == "__main__":
    main()
