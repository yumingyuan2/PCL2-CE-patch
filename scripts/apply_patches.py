#!/usr/bin/env python3
"""Apply the PCL2-CE-patch changes to an upstream PCL-CE source checkout.

The patches are deliberately explicit: if upstream refactors one of the
anchors below, this script fails the workflow instead of silently publishing
an unpatched build.

Usage:
    python3 scripts/apply_patches.py <upstream-source-dir> --repo owner/name
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

MARKER = "PCL2-CE-patch"
SKIP_DIRS = {".git", "bin", "obj", ".vs"}


class PatchError(RuntimeError):
    pass


def read_source(path: Path) -> tuple[str, str]:
    """Return (text, newline) while preserving the BOM and line endings."""
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    newline = "\r\n" if "\r\n" in text else "\n"
    return text, newline


def write_source(path: Path, text: str, had_bom: bool) -> None:
    data = text.encode("utf-8")
    if had_bom:
        data = b"\xef\xbb\xbf" + data
    path.write_bytes(data)


def read_plain(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def find_source(root: Path, filename: str, needle: str | None = None) -> Path:
    matches = [
        path
        for path in root.rglob(filename)
        if not any(part in SKIP_DIRS for part in path.parts)
    ]
    if needle is not None:
        matches = [path for path in matches if needle in read_plain(path)]
    if not matches:
        raise PatchError(f"Could not find {filename!r} in {root}")
    if len(matches) > 1:
        raise PatchError(
            f"Found multiple candidates for {filename!r}: "
            + ", ".join(str(path.relative_to(root)) for path in matches)
        )
    return matches[0]


def sub_once(
    text: str,
    pattern: str,
    replacement: str,
    description: str,
    *,
    flags: int = re.MULTILINE,
) -> tuple[str, bool]:
    new_text, count = re.subn(pattern, replacement, text, count=1, flags=flags)
    if count != 1:
        raise PatchError(f"Patch anchor not found: {description}")
    return new_text, True


def patch_right_hint(root: Path) -> None:
    path = find_source(root, "PageLaunchRight.xaml.cs", "PanHint")
    text, newline = read_source(path)
    marker = "// PCL2-CE-patch: hide the CE notice on the right"

    if marker not in text:
        # The notice is a MyCard named PanHint.  Its visibility is the only
        # thing that needs to be forced to Collapsed.
        expression = (
            r"(?P<indent>[ \t]*)PanHint\.Visibility\s*=\s*States\.Hint\.CEMessage\s*\r?\n"
            r"[ \t]*\?\s*Visibility\.Visible\s*\r?\n"
            r"[ \t]*:\s*Visibility\.Collapsed\s*;"
        )
        replacement = (
            r"\g<indent>PanHint.Visibility = Visibility.Collapsed; "
            "// PCL2-CE-patch: hide the CE notice on the right"
        )
        text, _ = sub_once(text, expression, replacement, "PageLaunchRight.PanHint.Visibility")

    write_source(path, text, path.read_bytes().startswith(b"\xef\xbb\xbf"))

    # Also flip the persisted default, so a fresh profile never enables it.
    # This is secondary to the forced Collapsed above and is therefore
    # allowed to be absent in a future refactor.
    try:
        states_path = find_source(root, "States.cs", "UiLauncherCEHint")
        states_text, _ = read_source(states_path)
        states_pattern = r'(\[ConfigItem<bool>\("UiLauncherCEHint",\s*)true(\s*\)\])'
        states_new, count = re.subn(states_pattern, r"\1false\2", states_text, count=1)
        if count == 1:
            write_source(states_path, states_new, states_path.read_bytes().startswith(b"\xef\xbb\xbf"))
            print(f"  patched {states_path.relative_to(root)} (default=false)")
        else:
            print("  note: UiLauncherCEHint default was already changed; skipped")
    except PatchError:
        print("  note: UiLauncherCEHint default not found; the forced Collapsed patch is still active")

    print(f"  patched {path.relative_to(root)} (right-side CE notice hidden)")


def patch_region_policy(root: Path) -> None:
    try:
        region_path = find_source(root, "RegionPolicy.cs", "RegionAssessment")
    except PatchError:
        region_path = None

    central_patched = False
    if region_path is not None:
        text, _ = read_source(region_path)
        marker = "public bool IsRestricted => false; // PCL2-CE-patch"
        central_patched = marker in text

        if not central_patched:
            pattern = (
                r"public\s+bool\s+IsRestricted\s*=>\s*Classification\s+is\s+"
                r"RegionClassification\.Mainland\s+or\s+RegionClassification\.Unknown\s*;"
            )
            replacement = f"{marker}: allow Minecraft downloads in every region"
            try:
                text, central_patched = sub_once(
                    text,
                    pattern,
                    replacement,
                    "RegionAssessment.IsRestricted",
                )
            except PatchError:
                central_patched = False

        write_source(region_path, text, region_path.read_bytes().startswith(b"\xef\xbb\xbf"))

    policy_is_restricted_patched = False
    check_patched = False
    try:
        policy_path = find_source(root, "RestrictedFeaturePolicy.cs", "RestrictedFeaturePolicy")
    except PatchError:
        policy_path = None

    if policy_path is not None:
        policy_text, _ = read_source(policy_path)
        policy_marker = "internal static bool IsRestricted => false; // PCL2-CE-patch"
        policy_is_restricted_patched = policy_marker in policy_text

        if not policy_is_restricted_patched:
            pattern = r"internal\s+static\s+bool\s+IsRestricted\s*=>\s*Assess\(\)\.IsRestricted\s*;"
            try:
                policy_text, policy_is_restricted_patched = sub_once(
                    policy_text,
                    pattern,
                    policy_marker,
                    "RestrictedFeaturePolicy.IsRestricted",
                )
            except PatchError:
                policy_is_restricted_patched = False

        # Fallback for a future refactor that removes RegionAssessment.IsRestricted
        # but keeps the central Check helper.  The current code does not need this.
        check_patched = "// PCL2-CE-patch: region gate disabled" in policy_text
        if not central_patched and not check_patched:
            pattern = (
                r"(internal\s+static\s+bool\s+Check\(string feature, bool showHint = true\)\s*\{\s*\r?\n)"
                r"([ \t]*)var assessment = Assess\(\);\s*\r?\n"
                r"[ \t]*if \(!assessment\.IsRestricted\) return true;"
            )
            replacement = (
                r"\1\2return true; // PCL2-CE-patch: region gate disabled"
            )
            try:
                policy_text, check_patched = sub_once(
                    policy_text,
                    pattern,
                    replacement,
                    "RestrictedFeaturePolicy.Check",
                )
            except PatchError:
                check_patched = False

        write_source(policy_path, policy_text, policy_path.read_bytes().startswith(b"\xef\xbb\xbf"))

    if region_path is None and policy_path is None:
        # Older betas predate the region policy entirely.  There is no gate to
        # disable, so this patch is a no-op for them.
        print("  note: no region policy found (older beta); MC downloads are already unrestricted")
        return

    if not (central_patched or (policy_is_restricted_patched and check_patched)):
        raise PatchError(
            "Could not disable the regional feature gate. "
            "Neither RegionAssessment.IsRestricted nor the RestrictedFeaturePolicy fallbacks matched."
        )

    if region_path is not None:
        print(f"  patched {region_path.relative_to(root)} (region gate disabled)")
    else:
        print("  patched RestrictedFeaturePolicy (region gate disabled)")


UPDATER_SOURCE = r'''using System.Text.Json.Nodes;
using PCL.Core.App.Localization;
using PCL.Core.Utils;
using PCL.Network;
using PCL.Network.Loaders;

namespace PCL;

/// <summary>
/// Reads releases from the PCL2-CE-patch repository.  Only assets ending in
/// "_patched.exe" are considered, so an accidentally uploaded upstream binary
/// can never be offered as an update.
/// </summary>
public class UpdatesPclCePatchModel : IUpdateSource
{
    private const string ReleasesApi =
        "https://api.github.com/repos/__REPOSITORY__/releases?per_page=100";
    private const string PatchedSuffix = "__RELEASE_SUFFIX__";

    private JsonArray? _releases;

    public string SourceName { get; set; } = "PCL2-CE-Patch";

    public bool IsAvailable() => true;

    public bool RefreshCache()
    {
        var json = Requester.FetchJson(ReleasesApi, new RequestParam
        {
            Accept = "application/vnd.github+json",
            UseBrowserUserAgent = true,
            Timeout = 30000,
            Retries = 2
        });
        _releases = json as JsonArray
                    ?? throw new Exception("Patched update source returned an invalid response.");
        return true;
    }

    public VersionDataModel GetLatestVersion(UpdateChannel channel, UpdateArch arch)
    {
        var release = FindRelease(channel, arch, out var asset);
        return new VersionDataModel
        {
            VersionName = GetVersionName(release),
            VersionCode = 0,
            Sha256 = GetSha256(asset),
            Source = SourceName,
            Changelog = release["body"]?.ToString() ?? ""
        };
    }

    public bool IsLatest(UpdateChannel channel, UpdateArch arch, SemVer currentVersion, int currentVersionCode)
    {
        var latest = GetLatestVersion(channel, arch);
        return currentVersion >= SemVer.Parse(latest.VersionName);
    }

    public VersionAnnouncementDataModel GetAnnouncementList()
    {
        // Announcements remain an upstream-only feature.  Returning an empty
        // list keeps AnnouncementService happy without re-enabling upstream
        // version downloads.
        return new VersionAnnouncementDataModel
        {
            Content = new List<VersionAnnouncementContentModel>()
        };
    }

    public List<ModLoader.LoaderBase> GetDownloadLoader(UpdateChannel channel, UpdateArch arch, string output)
    {
        var release = FindRelease(channel, arch, out var asset);
        var url = asset["browser_download_url"]?.ToString()
                  ?? throw new Exception("Patched release asset has no download URL.");

        var loaders = new List<ModLoader.LoaderBase>();
        loaders.Add(new ModLoader.LoaderTask<int, List<DownloadFile>>(
            Lang.Text("Update.Task.GetDownloadInfo"), load =>
            {
                load.output = new List<DownloadFile> { new(new[] { url }, output) };
            }));
        loaders.Add(new LoaderDownload(Lang.Text("Update.Task.DownloadUpdateFile"), new List<DownloadFile>()));
        return loaders;
    }

    private JsonObject FindRelease(UpdateChannel channel, UpdateArch arch, out JsonObject asset)
    {
        if (_releases is null)
            RefreshCache();

        var wantPrerelease = channel == UpdateChannel.beta;
        JsonObject? selected = null;
        JsonObject? selectedAsset = null;
        SemVer? selectedVersion = null;

        foreach (var node in _releases!)
        {
            if (node is not JsonObject release)
                continue;
            if ((bool?)release["draft"] ?? false)
                continue;
            if (((bool?)release["prerelease"] ?? false) != wantPrerelease)
                continue;

            var tag = release["tag_name"]?.ToString() ?? "";
            if (!tag.StartsWith('v') ||
                !tag.EndsWith(PatchedSuffix, StringComparison.OrdinalIgnoreCase))
                continue;

            var version = tag[1..^PatchedSuffix.Length];
            if (!SemVer.TryParse(version, out var parsedVersion))
                continue;

            var found = FindAsset(release, channel, arch);
            if (found is null)
                continue;

            // Pick the highest version instead of trusting API ordering.  This
            // keeps updates correct even if an older release is rebuilt later.
            if (selectedVersion is not null && parsedVersion <= selectedVersion)
                continue;

            selected = release;
            selectedAsset = found;
            selectedVersion = parsedVersion;
        }

        if (selected is not null && selectedAsset is not null)
        {
            asset = selectedAsset;
            return selected;
        }

        throw new Exception(
            $"No patched {(wantPrerelease ? "beta" : "stable")} release with a {arch} asset was found.");
    }

    private static JsonObject? FindAsset(JsonObject release, UpdateChannel channel, UpdateArch arch)
    {
        var configuration = channel == UpdateChannel.beta ? "Beta" : "Release";
        var architecture = arch == UpdateArch.arm64 ? "ARM64" : "x64";
        var expected = $"__ASSET_PREFIX___{configuration}_{architecture}_patched.exe";

        if (release["assets"] is not JsonArray assets)
            return null;

        return assets.OfType<JsonObject>().FirstOrDefault(asset =>
            string.Equals(asset["name"]?.ToString(), expected, StringComparison.OrdinalIgnoreCase));
    }

    private static string GetVersionName(JsonObject release)
    {
        var tag = release["tag_name"]!.ToString();
        return tag[1..^PatchedSuffix.Length];
    }

    private static string GetSha256(JsonObject asset)
    {
        var digest = asset["digest"]?.ToString();
        if (string.IsNullOrWhiteSpace(digest))
            throw new Exception("Patched release asset has no sha256 digest.");

        if (digest.StartsWith("sha256:", StringComparison.OrdinalIgnoreCase))
            digest = digest[7..];
        if (digest.Length != 64)
            throw new Exception("Patched release asset has an invalid sha256 digest.");

        return digest.ToLowerInvariant();
    }
}
'''


def patch_profile_creation_unlock(root: Path) -> None:
    path = find_source(root, "ProfileUi.cs", "CanCreateOtherProfile")
    text, _ = read_source(path)
    marker = "return true; // PCL2-CE-patch: allow profile types without a Microsoft login"

    primary_ok = marker in text
    if not primary_ok:
        expression = (
            r"return\s+ProfileService\.HasMicrosoftProfile\s*\|\|\s*"
            r"\(Lang\.IsFeaturesUnrestricted\s*&&\s*ProfileService\.Profiles\.Count\s*>\s*0\)\s*\|\|\s*"
            r"NetworkHelper\.IsNetworkAvailable\(\)\s+is\s+false\s*;"
        )
        try:
            text, primary_ok = sub_once(
                text,
                expression,
                marker,
                "ProfileUi.CanCreateOtherProfile",
            )
        except PatchError:
            primary_ok = False

    if not primary_ok:
        # Fallback: force the only caller to show all profile types.
        fallback = r"(var\s+includeOthers\s*=\s*)CanCreateOtherProfile\(\)\s*;"
        try:
            text, fallback_ok = sub_once(
                text,
                fallback,
                r"\1true; // PCL2-CE-patch: allow profile types without a Microsoft login",
                "ProfileUi.CreateProfile includeOthers",
            )
        except PatchError:
            fallback_ok = False
        if not fallback_ok:
            raise PatchError("Could not unlock offline/third-party profile creation")

    write_source(path, text, path.read_bytes().startswith(b"\xef\xbb\xbf"))
    print(f"  patched {path.relative_to(root)} (all profile types allowed without Microsoft login)")

    # The yellow hint says "verify with Microsoft once before using other
    # methods".  That is no longer true after the patch, so hide it.
    try:
        hint_path = find_source(root, "PageLoginProfile.xaml.cs", "HintMicrosoft")
        hint_text, _ = read_source(hint_path)
        hint_marker = "// PCL2-CE-patch: no Microsoft login required"
        if hint_marker not in hint_text:
            pattern = (
                r"(?m)^(?P<indent>[ \t]*)HintMicrosoft\.Visibility\s*=\s*"
                r"ProfileService\.Profiles\.Count\s*==\s*0\s*\?\s*Visibility\.Visible\s*:\s*Visibility\.Collapsed\s*;"
            )
            replacement = (
                r"\g<indent>HintMicrosoft.Visibility = Visibility.Collapsed; "
                "// PCL2-CE-patch: no Microsoft login required"
            )
            hint_text, _ = sub_once(
                hint_text,
                pattern,
                replacement,
                "PageLoginProfile.HintMicrosoft.Visibility",
            )
            write_source(hint_path, hint_text, hint_path.read_bytes().startswith(b"\xef\xbb\xbf"))
            print(f"  patched {hint_path.relative_to(root)} (removed Microsoft-first hint)")
    except PatchError:
        print("  note: Microsoft-first hint was not found; skipped")


def patch_remove_prompts(root: Path) -> None:
    try:
        path = find_source(root, "ModLaunch.cs", "McLaunchStart")
    except PatchError:
        print("  note: ModLaunch.McLaunchStart not found; prompts unchanged")
        return

    text, _ = read_source(path)
    marker = "// PCL2-CE-patch: purchase/donation prompts removed"
    if marker in text:
        return

    # Donation prompt (BETA builds, every N launches).
    donation = re.compile(r"#if BETA.*?Donate.*?#endif", re.DOTALL)
    text, donation_count = donation.subn(marker, text, count=1)
    if donation_count != 1:
        print("  note: donation prompt block not found; skipped")

    # Genuine-purchase / account-verification dialog.
    start = text.find("// 正版购买提示")
    purchase_removed = False
    if start >= 0:
        condition = text.find("if (!ProfileService.HasMicrosoftProfile)", start)
        if condition >= 0:
            brace = text.find("{", condition)
            depth = 0
            close = -1
            for index in range(brace, len(text)):
                char = text[index]
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if depth == 0:
                        close = index
                        break
            if close >= 0:
                text = text[:start] + marker + text[close + 1 :]
                purchase_removed = True
    if not purchase_removed:
        print("  note: purchase prompt block not found; skipped")

    leftovers = [
        key
        for key in (
            "Minecraft.Launch.PurchaseHint",
            "Minecraft.Launch.AccountVerification",
            "Minecraft.Launch.Donate",
        )
        if key in text
    ]
    if leftovers:
        raise PatchError(
            "Prompt removal incomplete; still found: " + ", ".join(leftovers)
        )

    write_source(path, text, path.read_bytes().startswith(b"\xef\xbb\xbf"))
    print(f"  patched {path.relative_to(root)} (purchase/donation prompts removed)")


def patch_ce_announce(root: Path) -> None:
    try:
        path = find_source(root, "UpdateManager.cs", "ShowCEAnnounce")
    except PatchError:
        print("  note: ShowCEAnnounce not found; nothing to suppress")
        return

    text, newline = read_source(path)
    marker = "// PCL2-CE-patch: suppress the CE announcement after an update"
    if marker in text:
        return

    start = text.find("public static void ShowCEAnnounce")
    if start < 0:
        print("  note: ShowCEAnnounce signature not found; skipped")
        return

    brace = text.find("{", start)
    if brace < 0:
        raise PatchError("Could not find the ShowCEAnnounce body")
    depth = 0
    close = -1
    for index in range(brace, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                close = index
                break
    if close < 0:
        raise PatchError("Could not find the end of ShowCEAnnounce")

    line_start = text.rfind("\n", 0, start) + 1
    indent = text[line_start:start]
    signature = text[start:brace]
    replacement = newline.join(
        [
            signature.rstrip(),
            f"{indent}{{",
            f"{indent}    {marker}",
            f"{indent}}}",
        ]
    )
    text = text[:start] + replacement + text[close + 1 :]
    write_source(path, text, path.read_bytes().startswith(b"\xef\xbb\xbf"))
    print(f"  patched {path.relative_to(root)} (suppressed CE update announcement)")


def patch_oe_shell(root: Path) -> None:
    """First visible layer of the OE UI: title, CE badge, fourth tab."""
    main_path = find_source(root, "FormMain.xaml", "CELogo")
    text, newline = read_source(main_path)
    marker = "<!-- PCL2-CE-patch: OE shell -->"

    if marker not in text:
        text = marker + newline + text
        try:
            text, _ = sub_once(
                text,
                r'Title="Plain Craft Launcher Community Edition\s*"',
                'Title="PCL2 OE"',
                "FormMain title",
            )
        except PatchError:
            pass

        # Hide the CE badge in XAML; ModSetup will also be patched below.
        text = re.sub(
            r'(<Border x:Name="CELogo")(?![^>]*\bVisibility=)',
            r'\1 Visibility="Collapsed"',
            text,
            count=1,
        )

        # Official fourth tab is "More" with a grid icon.
        text = text.replace('SvgIcon="lucide/wrench"', 'SvgIcon="lucide/grid-2x2"', 1)
        write_source(main_path, text, main_path.read_bytes().startswith(b"\xef\xbb\xbf"))
        print(f"  patched {main_path.relative_to(root)} (OE title / CE badge / More tab)")

    # Hide the badge wherever the logo style is applied at runtime.
    try:
        setup_path = find_source(root, "ModSetup.cs", "CELogo")
        setup_text, _ = read_source(setup_path)
        setup_marker = "// PCL2-CE-patch: OE hides the CE badge"
        if setup_marker not in setup_text:
            setup_text = setup_text.replace(
                "ModMain.frmMain.CELogo.Visibility = Visibility.Visible;",
                "ModMain.frmMain.CELogo.Visibility = Visibility.Collapsed; " + setup_marker,
            )
            write_source(setup_path, setup_text, setup_path.read_bytes().startswith(b"\xef\xbb\xbf"))
            print(f"  patched {setup_path.relative_to(root)} (CE badge hidden)")
    except PatchError:
        pass

    # Localized label for the fourth top-level tab.
    labels = {
        "zh-CN.xaml": "更多",
        "zh-TW.xaml": "更多",
        "en-US.xaml": "More",
        "en-GB.xaml": "More",
    }
    for filename, value in labels.items():
        try:
            path = find_source(root, filename, "Main.Tab.Tools")
        except PatchError:
            continue
        text, _ = read_source(path)
        pattern = r'(<sys:String x:Key="Main\.Tab\.Tools">)[^<]*(</sys:String>)'
        text, count = re.subn(pattern, rf"\g<1>{value}\2", text, count=1)
        if count == 1:
            write_source(path, text, path.read_bytes().startswith(b"\xef\xbb\xbf"))
            print(f"  patched {path.relative_to(root)} (Tools -> More)")


def patch_updater(
    root: Path,
    repository: str,
    asset_prefix: str,
    release_suffix: str,
) -> None:
    updater_path = find_source(root, "UpdateManager.cs", "remoteServer")
    text, newline = read_source(updater_path)

    start = text.find("public static UpdatesWrapperModel remoteServer")
    if start < 0:
        raise PatchError("Could not find UpdateManager.remoteServer")

    list_start = text.find("new(new List<IUpdateSource>", start)
    if list_start < 0:
        raise PatchError("Could not find the remoteServer source list")

    brace_start = text.find("{", list_start)
    if brace_start < 0:
        raise PatchError("Could not find the start of the remoteServer source list")

    depth = 0
    close = -1
    for index in range(brace_start, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                close = index
                break
    if close < 0:
        raise PatchError("Could not find the end of the remoteServer source list")

    semicolon = text.find(";", close)
    if semicolon < 0:
        raise PatchError("Could not find the end of the remoteServer declaration")

    replacement = newline.join(
        [
            "public static UpdatesWrapperModel remoteServer = new(new List<IUpdateSource>",
            "    {",
            "        // PCL2-CE-patch: only use patched builds from this repository.",
            "        new UpdatesPclCePatchModel()",
            "    });",
        ]
    )
    text = text[:start] + replacement + text[semicolon + 1 :]
    write_source(updater_path, text, updater_path.read_bytes().startswith(b"\xef\xbb\xbf"))

    class_text = (
        UPDATER_SOURCE.replace("__REPOSITORY__", repository)
        .replace("__ASSET_PREFIX__", asset_prefix)
        .replace("__RELEASE_SUFFIX__", release_suffix)
    )
    class_path = updater_path.with_name("UpdatesPclCePatchModel.cs")
    class_path.write_text(class_text, encoding="utf-8", newline="\n")

    print(f"  patched {updater_path.relative_to(root)} (updater points at {repository})")
    print(f"  added   {class_path.relative_to(root)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Path to the upstream PCL-CE checkout")
    parser.add_argument(
        "--repo",
        required=True,
        help="GitHub repository that publishes the patched releases, e.g. owner/PCL2-CE-patch",
    )
    parser.add_argument(
        "--edition",
        choices=("ce", "oe"),
        default="ce",
        help="ce: keep CE branding; oe: apply the first OE shell layer",
    )
    args = parser.parse_args()

    root = args.source.resolve()
    if not (root / "Plain Craft Launcher 2.slnx").exists():
        print(f"error: {root} does not look like a PCL-CE checkout", file=sys.stderr)
        return 2

    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repo):
        print(f"error: invalid repository name: {args.repo}", file=sys.stderr)
        return 2

    print(f"Applying PCL2-CE patches to {root} ...")
    try:
        patch_right_hint(root)
        patch_region_policy(root)
        patch_profile_creation_unlock(root)
        patch_ce_announce(root)
        patch_remove_prompts(root)
        if args.edition == "oe":
            patch_oe_shell(root)
        asset_prefix = "PCL2_OE" if args.edition == "oe" else "PCL2_CE"
        release_suffix = "-oe-patched" if args.edition == "oe" else "-patched"
        patch_updater(root, args.repo, asset_prefix, release_suffix)
    except PatchError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    print("All patches applied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
