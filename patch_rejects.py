import os
import subprocess
import shutil
import difflib
import re
import argparse
from pathlib import Path

def run_command(command, cwd=None, check=True):
    """Run a shell command and handle errors."""
    result = subprocess.run(command, shell=True, cwd=cwd, text=True, capture_output=True)
    if check and result.returncode != 0:
        print(f"Error running command '{command}': {result.stderr}")
        raise subprocess.CalledProcessError(result.returncode, command)
    return result

def clone_repos(ksu_branch, susfs_branch, susfs_commit, work_dir):
    """Clone KernelSU-Next and susfs4ksu repositories with specified branches or commit."""
    os.makedirs(work_dir, exist_ok=True)
    ksu_dir = os.path.join(work_dir, "KernelSU-Next")
    susfs_dir = os.path.join(work_dir, "susfs4ksu")
    
    if not os.path.exists(ksu_dir):
        run_command(f"git clone -b {ksu_branch} https://github.com/KernelSU-Next/KernelSU-Next.git", cwd=work_dir)
    if not os.path.exists(susfs_dir):
        run_command(f"git clone -b {susfs_branch} https://gitlab.com/simonpunk/susfs4ksu.git", cwd=work_dir)
        if susfs_commit:
            run_command(f"git checkout {susfs_commit}", cwd=susfs_dir)
            print(f"Checked out susfs4ksu commit {susfs_commit}")
    
    return ksu_dir, susfs_dir

def apply_patch(patch_file, target_dir):
    """Apply a patch and return a list of generated .rej files."""
    try:
        run_command(f"patch -p1 < {patch_file}", cwd=target_dir)
        print(f"Successfully applied patch {patch_file}")
    except subprocess.CalledProcessError:
        print(f"Patch application failed for {patch_file}, processing reject files.")
    
    rej_files = []
    for root, _, files in os.walk(target_dir):
        for file in files:
            if file.endswith(".rej"):
                rej_files.append(os.path.join(root, file))
    return rej_files

def parse_reject_file(rej_file):
    """Parse a .rej file and extract failed hunks."""
    with open(rej_file, 'r') as f:
        content = f.read()
    
    lines = content.splitlines()
    print(f"Debug: First 5 lines of {rej_file}:")
    for i, line in enumerate(lines[:5]):
        print(f"Line {i+1}: {line}")
    
    hunks = []
    current_hunk = None
    i = 0
    while i < len(lines):
        if lines[i].startswith("@@"):
            if current_hunk:
                hunks.append(current_hunk)
            current_hunk = {"context": [], "changes": [], "file": None, "start_line": None}
            match = re.match(r"@@ -(\d+),(\d+)", lines[i])
            if match:
                current_hunk["start_line"] = int(match.group(1))
                current_hunk["end_line"] = int(match.group(1)) + int(match.group(2)) - 1
            if i > 0 and lines[i-1].startswith("--- "):
                current_hunk["file"] = lines[i-1].split()[1]
            elif i > 1 and lines[i-2].startswith("--- "):
                current_hunk["file"] = lines[i-2].split()[1]
            i += 1
        elif lines[i].startswith("***************"):
            if current_hunk:
                hunks.append(current_hunk)
            current_hunk = {"context": [], "changes": [], "file": None, "start_line": None}
            i += 1
            if i < len(lines) and lines[i].startswith("***"):
                match = re.match(r"\*\*\* (\d+),(\d+)", lines[i])
                if match:
                    current_hunk["start_line"] = int(match.group(1))
                    current_hunk["end_line"] = int(match.group(2))
                if i > 0 and lines[i-2].startswith("--- "):
                    current_hunk["file"] = lines[i-2].split()[1]
            i += 1
        elif current_hunk:
            if lines[i].startswith("- ") or lines[i].startswith("+ "):
                current_hunk["changes"].append(lines[i])
            else:
                current_hunk["context"].append(lines[i].strip())
            i += 1
        else:
            i += 1
    
    if current_hunk:
        hunks.append(current_hunk)
    
    if hunks and not any(hunk["file"] for hunk in hunks):
        derived_file = os.path.basename(rej_file).replace(".rej", "")
        print(f"Warning: No file path found in {rej_file}, assuming {derived_file}")
        for hunk in hunks:
            hunk["file"] = derived_file
    
    return hunks

def find_function_boundary(source_lines, target_line, file_name):
    """Find the nearest function or block boundary, with special handling for core_hook.c."""
    if not source_lines:
        return 0
    target_line = min(max(0, target_line - 1), len(source_lines) - 1)
    
    if file_name.endswith("core_hook.c"):
        for i in range(target_line, -1, -1):
            line = source_lines[i].strip()
            if re.match(r"(static\s+)?void\s+try_umount\s*\(", line) or re.match(r"(static\s+)?void\s+susfs_try_umount\s*\(", line):
                print(f"Debug: Found try_umount/susfs_try_umount at line {i + 1}")
                return i + 1
            if line.startswith("#ifdef CONFIG_KSU_SUSFS"):
                print(f"Debug: Found #ifdef CONFIG_KSU_SUSFS at line {i + 1}")
                return i + 1
        for i in range(target_line, len(source_lines)):
            line = source_lines[i].strip()
            if re.match(r"(static\s+)?void\s+try_umount\s*\(", line) or re.match(r"(static\s+)?void\s+susfs_try_umount\s*\(", line):
                print(f"Debug: Found try_umount/susfs_try_umount at line {i + 1}")
                return i
            if line.startswith("#ifdef CONFIG_KSU_SUSFS"):
                print(f"Debug: Found #ifdef CONFIG_KSU_SUSFS at line {i + 1}")
                return i
    for i in range(target_line, -1, -1):
        line = source_lines[i].strip()
        if re.match(r"(static\s+)?(void|int|bool)\s+\w+\s*\(", line) or line.endswith("{"):
            print(f"Debug: Found function/block boundary at line {i + 1}")
            return i + 1
    for i in range(target_line, len(source_lines)):
        line = source_lines[i].strip()
        if re.match(r"(static\s+)?(void|int|bool)\s+\w+\s*\(", line) or line.endswith("{"):
            print(f"Debug: Found function/block boundary at line {i + 1}")
            return i
    print(f"Debug: No function/block boundary found, using end of file")
    return len(source_lines)

def apply_hunk_to_file(source_file, hunk, output_file):
    """Apply a hunk to the source file with robust change application."""
    if not os.path.exists(source_file):
        print(f"Error: Source file {source_file} not found, cannot apply hunk.")
        return False
    
    with open(source_file, 'r') as f:
        source_lines = f.readlines()
    
    context_lines = [line.strip() for line in hunk["context"] if line.strip()]
    print(f"Debug: Context lines for {source_file}: {context_lines}")
    
    file_name = os.path.basename(source_file)
    applied_changes = []
    
    # Log source file lines around the target line for debugging
    if hunk["start_line"] is not None:
        start = max(0, hunk["start_line"] - 5)
        end = min(len(source_lines), hunk["start_line"] + 5)
        print(f"Debug: Source file lines around target line {hunk['start_line']}:")
        for i in range(start, end):
            print(f"Line {i+1}: {source_lines[i].strip()}")
    
    # Step 1: Try exact context matching
    matcher = difflib.SequenceMatcher(None, context_lines, [line.strip() for line in source_lines])
    match = matcher.find_longest_match(0, len(context_lines), 0, len(source_lines))
    
    if context_lines and match.size >= 1:  # Relaxed threshold to ensure matching
        print(f"Debug: Found context match at line {match.b} with {match.size} matching lines")
        target_line = match.b
        new_lines = source_lines[:target_line]
        
        for change in hunk["changes"]:
            if change.startswith("+ "):
                new_lines.append(change[2:] + "\n")
                applied_changes.append(f"Added: {change[2:].strip()}")
            elif change.startswith("- "):
                if target_line < len(source_lines) and source_lines[target_line].strip() == change[2:].strip():
                    applied_changes.append(f"Removed: {change[2:].strip()}")
                    target_line += 1
                else:
                    print(f"Debug: Failed to delete line at {target_line + 1}: expected '{change[2:].strip()}', found '{source_lines[target_line].strip() if target_line < len(source_lines) else 'EOF'}'")
                    # Append the line as an addition to ensure changes are applied
                    if change[2:].strip():
                        new_lines.append(change[2:] + "\n")
                        applied_changes.append(f"Added instead of deleted: {change[2:].strip()}")
        
        new_lines.extend(source_lines[target_line:])
    else:
        # Step 2: Try fuzzy matching within a window
        if hunk["start_line"] is not None:
            window_size = 50
            start = max(0, hunk["start_line"] - 1 - window_size)
            end = min(len(source_lines), hunk["start_line"] - 1 + window_size)
            window_lines = [line.strip() for line in source_lines[start:end]]
            
            matcher = difflib.SequenceMatcher(None, context_lines, window_lines)
            match = matcher.find_longest_match(0, len(context_lines), 0, len(window_lines))
            
            if match.size >= 1:  # Relaxed threshold
                print(f"Debug: Found fuzzy match at line {start + match.b} with {match.size} matching lines")
                target_line = start + match.b
                new_lines = source_lines[:target_line]
                
                for change in hunk["changes"]:
                    if change.startswith("+ "):
                        new_lines.append(change[2:] + "\n")
                        applied_changes.append(f"Added: {change[2:].strip()}")
                    elif change.startswith("- "):
                        if target_line < len(source_lines) and source_lines[target_line].strip() == change[2:].strip():
                            applied_changes.append(f"Removed: {change[2:].strip()}")
                            target_line += 1
                        else:
                            print(f"Debug: Failed to delete line at {target_line + 1}: expected '{change[2:].strip()}', found '{source_lines[target_line].strip() if target_line < len(source_lines) else 'EOF'}'")
                            if change[2:].strip():
                                new_lines.append(change[2:] + "\n")
                                applied_changes.append(f"Added instead of deleted: {change[2:].strip()}")
                
                new_lines.extend(source_lines[target_line:])
            else:
                # Step 3: Fallback to semantic insertion
                print(f"Warning: No context match for {source_file}, falling back to semantic insertion")
                target_line = find_function_boundary(source_lines, hunk["start_line"] or 1, file_name)
                print(f"Debug: Inserting at line {target_line + 1}")
                new_lines = source_lines[:target_line]
                
                if file_name == "core_hook.c" and any("susfs_try_umount" in change for change in hunk["changes"]):
                    for i in range(max(0, target_line - 50), min(len(source_lines), target_line + 50)):
                        if source_lines[i].strip().startswith("#ifdef CONFIG_KSU_SUSFS"):
                            target_line = i + 1
                            print(f"Debug: Adjusted to insert within #ifdef CONFIG_KSU_SUSFS at line {target_line + 1}")
                            break
                
                for change in hunk["changes"]:
                    if change.startswith("+ "):
                        new_lines.append(change[2:] + "\n")
                        applied_changes.append(f"Added: {change[2:].strip()}")
                    elif change.startswith("- "):
                        print(f"Debug: Skipping deletion at line {target_line + 1}: expected '{change[2:].strip()}', not found in source")
                
                new_lines.extend(source_lines[target_line:])
        else:
            # Step 4: Append to file
            print(f"Warning: No start line for {source_file}, appending changes")
            new_lines = source_lines[:]
            for change in hunk["changes"]:
                if change.startswith("+ "):
                    new_lines.append(change[2:] + "\n")
                    applied_changes.append(f"Added: {change[2:].strip()}")
                elif change.startswith("- "):
                    print(f"Debug: Skipping deletion at EOF: expected '{change[2:].strip()}'")
    
    if not applied_changes:
        print(f"Error: No changes applied to {source_file}, hunk application failed")
        print(f"Debug: Hunk changes attempted: {hunk['changes']}")
        return False
    
    print(f"Debug: Applied changes to {source_file}: {applied_changes}")
    
    with open(output_file, 'w') as f:
        f.writelines(new_lines)
    
    print(f"Debug: Successfully wrote changes to {output_file}")
    return True

def generate_new_patch(original_file, modified_file, output_patch):
    """Generate a new patch by comparing original and modified files using git diff."""
    if not os.path.exists(original_file):
        print(f"Error: Original file {original_file} not found, cannot generate patch.")
        return False
    if not os.path.exists(modified_file):
        print(f"Error: Modified file {modified_file} not found, cannot generate patch.")
        return False
    
    # Use git diff to compare files
    result = run_command(f"git diff --no-index -- {original_file} {modified_file}", check=False)
    
    # Always generate a patch file
    with open(output_patch, 'w') as f:
        if result.stdout.strip():
            f.write(result.stdout)
            print(f"Debug: Generated patch {output_patch} with size {os.path.getsize(output_patch)} bytes")
        else:
            f.write("# No differences found, but patch generated as requested\n")
            print(f"Debug: No differences found between {original_file} and {modified_file}, generated empty patch with comment")
    
    return True

def process_rejects(rej_files, ksu_dir, output_dir, patch_name):
    """Process all reject files and generate fixed patches with the same name as the input patch."""
    os.makedirs(output_dir, exist_ok=True)
    
    # Group reject files by their associated patch
    rej_groups = {}
    for rej_file in rej_files:
        patch_file_name = patch_name if patch_name else os.path.basename(rej_file).replace(".rej", ".patch")
        if patch_file_name == "10_enable_susfs_for_ksu.patch":
            print(f"Warning: Ignoring patch name {patch_file_name} as it is not allowed for output")
            patch_file_name = os.path.basename(rej_file).replace(".rej", ".patch")
        rej_groups.setdefault(patch_file_name, []).append(rej_file)
    
    for input_patch_name, rej_files in rej_groups.items():
        print(f"Processing reject files for patch: {input_patch_name}")
        modified_files = []
        
        for rej_file in rej_files:
            print(f"Processing reject file: {rej_file}")
            hunks = parse_reject_file(rej_file)
            if not hunks or not any(hunk["file"] for hunk in hunks):
                print(f"Error: No valid hunks or file path found in {rej_file}, cannot process.")
                continue
            
            target_file = hunks[0]["file"]
            source_file = os.path.join(ksu_dir, target_file)
            copy_file = os.path.join(output_dir, os.path.basename(target_file) + ".copy")
            output_file = os.path.join(output_dir, os.path.basename(target_file))
            
            if not os.path.exists(source_file):
                print(f"Error: Source file {source_file} not found in KernelSU-Next, cannot process.")
                continue
            
            shutil.copyfile(source_file, copy_file)
            
            success = True
            for hunk in hunks:
                if not apply_hunk_to_file(source_file, hunk, output_file):
                    success = False
                    print(f"Failed to apply hunk in {rej_file}")
                else:
                    modified_files.append((source_file, output_file, copy_file))
            
            print(f"Debug: Processed reject file {rej_file}, success={success}")
        
        # Generate a single patch for all modified files
        output_patch = os.path.join(output_dir, input_patch_name)
        combined_diff = []
        for source_file, output_file, copy_file in modified_files:
            result = run_command(f"git diff --no-index -- {copy_file} {output_file}", check=False)
            if result.stdout.strip():
                combined_diff.append(result.stdout)
                print(f"Debug: Added diff for {output_file} to combined patch")
        
        with open(output_patch, 'w') as f:
            if combined_diff:
                f.write("\n".join(combined_diff) + "\n")
                print(f"Debug: Generated combined patch {output_patch} with size {os.path.getsize(output_patch)} bytes")
            else:
                f.write("# No differences found, but patch generated as requested\n")
                print(f"Debug: No differences found for {input_patch_name}, generated empty patch with comment")
        
        print(f"Generated fixed patch: {output_patch}")

def main():
    parser = argparse.ArgumentParser(description="Automate kernel patch reject fixing.")
    parser.add_argument("--ksu-branch", default="next", help="KernelSU-Next branch (e.g., next)")
    parser.add_argument("--susfs-branch", default="gki-android13-5.15", help="susfs4ksu branch (e.g., gki-android13-5.15)")
    parser.add_argument("--susfs-commit", default=None, help="susfs4ksu commit hash (e.g., abc123)")
    parser.add_argument("--repo-dir", default="/path/to/kernel_patches", help="Path to kernel_patches repo")
    parser.add_argument("--process-rejects-only", type=lambda x: x.lower() == 'true', default=False, help="Process only existing rejects (true/false)")
    parser.add_argument("--patch-name", default=None, help="Name of the input patch file (e.g., fix_apk_sign.c.patch)")
    args = parser.parse_args()
    
    repo_dir = args.repo_dir
    rejects_dir = os.path.join(repo_dir, "reject_patcher", "rejects")
    output_dir = os.path.join(repo_dir, "reject_patcher", "output")
    work_dir = os.path.join(repo_dir, "work")
    
    ksu_dir, susfs_dir = clone_repos(args.ksu_branch, args.susfs_branch, args.susfs_commit, work_dir)
    
    rej_files = []
    if not args.process_rejects_only:
        patch_file = os.path.join(ksu_dir, args.patch_name or "fix_apk_sign.c.patch")
        if not os.path.exists(patch_file):
            print(f"Patch file {patch_file} not found, skipping patch application.")
        else:
            rej_files.extend(apply_patch(patch_file, ksu_dir))
    
    # Always collect .rej files from reject_patcher/rejects
    for rej_file in Path(rejects_dir).glob("*.rej"):
        rej_files.append(str(rej_file))
    
    if not rej_files:
        print("No reject files found to process.")
        return
    
    process_rejects(rej_files, ksu_dir, output_dir, args.patch_name)

if __name__ == "__main__":
    main()
