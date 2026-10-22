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


def patch_page_load_smoothness(root: Path) -> None:
    """Defer heavy first-load work so switching pages renders before it blocks."""
    download_path = find_source(root, "PageDownloadInstall.xaml.cs", "private void Init()")
    code, _ = read_source(download_path)
    marker = "// PCL2-CE-patch: defer download init"
    if marker not in code:
        if "using System.Windows.Threading;" not in code:
            code = code.replace(
                "using System.Windows;",
                "using System.Windows;\nusing System.Windows.Threading;",
                1,
            )
        code = code.replace(
            "    private void Init()",
            "    " + marker + "\n"
            "    private void Init()\n"
            "    {\n"
            "        Dispatcher.BeginInvoke(new Action(InitCore), DispatcherPriority.Background);\n"
            "    }\n"
            "\n"
            "    private void InitCore()",
            1,
        )
        write_source(download_path, code, download_path.read_bytes().startswith(b"\xef\xbb\xbf"))
        print(f"  patched {download_path.relative_to(root)} (defer download init)")

    setup_path = find_source(root, "PageSetupUI.xaml.cs", "Loaded += PageSetupUI_Loaded;")
    code, _ = read_source(setup_path)
    marker = "// PCL2-CE-patch: defer setup load"
    if marker not in code:
        if "using System.Windows.Threading;" not in code:
            code = code.replace(
                "using System.Windows;",
                "using System.Windows;\nusing System.Windows.Threading;",
                1,
            )
        code = code.replace(
            "        Loaded += PageSetupUI_Loaded;",
            "        " + marker + "\n"
            "        Loaded += (_, e) => Dispatcher.BeginInvoke(\n"
            "            new Action(() => PageSetupUI_Loaded(null, e)), DispatcherPriority.Background);",
            1,
        )
        write_source(setup_path, code, setup_path.read_bytes().startswith(b"\xef\xbb\xbf"))
        print(f"  patched {setup_path.relative_to(root)} (defer setup load)")


def patch_oe_launch_toggle(root: Path) -> None:
    """Recreate the official 正版/离线 pill switch above the login panel."""
    xaml_path = find_source(root, "PageLaunchLeft.xaml", "PanLogin")
    xaml, _ = read_source(xaml_path)
    marker = "<!-- PCL2-CE-patch: OE login type toggle -->"

    if 'xmlns:svgIcon="clr-namespace:PCL.Core.UI.Controls.SvgIcon;assembly=PCL.Core"' not in xaml:
        xaml = xaml.replace(
            'xmlns:local="clr-namespace:PCL"',
            'xmlns:local="clr-namespace:PCL"\n'
            '                  xmlns:svgIcon="clr-namespace:PCL.Core.UI.Controls.SvgIcon;assembly=PCL.Core"',
            1,
        )

    # Remove the earlier radio-box version if it exists, so preview builds
    # upgrade to the pill style as well.
    old = re.search(
        r"        <!-- PCL2-CE-patch: OE login type toggle -->.*?        </StackPanel>\r?\n",
        xaml,
        re.DOTALL,
    )
    if old is not None:
        xaml = xaml[: old.start()] + xaml[old.end() :]

    if marker not in xaml:
        anchor = '        <Grid Grid.ColumnSpan="5" Grid.Row="1" Margin="20,0" VerticalAlignment="Center" x:Name="PanLogin" />'
        block = "\n".join(
            [
                "        " + marker,
                '        <StackPanel x:Name="PanLoginToggle" Grid.Row="0" Grid.ColumnSpan="5"',
                '                    Orientation="Horizontal" HorizontalAlignment="Center" Margin="0,24,0,0">',
                '            <Border x:Name="BtnLoginMs" Height="28" CornerRadius="14" Padding="10,0"',
                '                    Margin="0,0" Background="#127AE1" Cursor="Hand"',
                '                    MouseLeftButtonUp="BtnLoginMs_Click">',
                "                <StackPanel Orientation=\"Horizontal\">",
                '                    <svgIcon:SvgIcon x:Name="IconLoginMs" Width="16" Height="16" Stretch="Uniform"',
                '                                     Icon="lucide/shield-check" IconBrush="White"',
                '                                     VerticalAlignment="Center" />',
                '                    <TextBlock x:Name="LabLoginMs" Text="正版" Foreground="White"',
                '                               Margin="6,0,0,0" FontSize="13.5" VerticalAlignment="Center" />',
                "                </StackPanel>",
                "            </Border>",
                '            <Border x:Name="BtnLoginOffline" Height="28" CornerRadius="14" Padding="10,0"',
                '                    Margin="12,0,0,0" Background="#EEF6FD" Cursor="Hand"',
                '                    MouseLeftButtonUp="BtnLoginOffline_Click">',
                "                <StackPanel Orientation=\"Horizontal\">",
                '                    <svgIcon:SvgIcon x:Name="IconLoginOffline" Width="16" Height="16" Stretch="Uniform"',
                '                                     Icon="lucide/link-2" IconBrush="#127AE1"',
                '                                     VerticalAlignment="Center" />',
                '                    <TextBlock x:Name="LabLoginOffline" Text="离线"',
                '                               Foreground="#127AE1"',
                '                               Margin="6,0,0,0" FontSize="13.5" VerticalAlignment="Center" />',
                "                </StackPanel>",
                "            </Border>",
                "        </StackPanel>",
                '        <StackPanel x:Name="PanLoginAuthType" Grid.Row="0" Grid.ColumnSpan="5"',
                '                    Orientation="Horizontal" HorizontalAlignment="Center" Margin="0,24,0,0"',
                '                    Visibility="Collapsed">',
                '            <Border Height="28" CornerRadius="14" Padding="12,0" Background="#EEF6FD">',
                "                <StackPanel Orientation=\"Horizontal\">",
                '                    <Path Width="16" Height="16" Stretch="Uniform" Fill="#127AE1" VerticalAlignment="Center"',
                '                          Data="M834.5 684.1c-31.2-70.4-98.9-120.9-179.1-127.3 63.5-8.5 112.6-63 112.6-128.8 0-71.8-58.2-130-130-130s-130 58.2-130 130c0 65.9 49 120.3 112.6 128.8-80.2 6.4-148 57-179.1 127.3-8.7 19.7 6 42 27.6 42 12.1 0 22.7-7.5 27.7-18.5 24.3-53.9 78.5-91.5 141.3-91.5s117 37.6 141.3 91.5c5 11.1 15.6 18.5 27.7 18.5 21.4 0 36.1-22.3 27.4-42zM567.9 427.9c0-38.6 31.4-70 70-70s70 31.4 70 70-31.4 70-70 70-70-31.4-70-70zM460.3 347.9H216.9c-16.6 0-30 13.4-30 30s13.4 30 30 30h243.3c16.6 0 30-13.4 30-30 0.1-16.5-13.4-30-29.9-30zM367.4 459.6H216.9c-16.6 0-30 13.4-30 30s13.4 30 30 30h150.4c16.6 0 30-13.4 30-30 0.1-16.6-13.4-30-29.9-30zM297.4 571.2H217c-16.6 0-30 13.4-30 30s13.4 30 30 30h80.4c16.6 0 30-13.4 30-30 0-16.5-13.5-30-30-30zM900 236v552H124V236h776m0-60H124c-33.1 0-60 26.9-60 60v552c0 33.1 26.9 60 60 60h776c33.1 0 60-26.9 60-60V236c0-33.1-26.9-60-60-60z" />',
                '                    <TextBlock x:Name="LabLoginAuthType" Text="第三方登录" Foreground="#127AE1"',
                '                               Margin="6,0,0,0" FontSize="13.5" VerticalAlignment="Center" />',
                "                </StackPanel>",
                "            </Border>",
                "        </StackPanel>",
                "",
            ]
        )
        if anchor not in xaml:
            raise PatchError("Could not find the OE launch login anchor")
        xaml = xaml.replace(anchor, block + anchor, 1)
        write_source(xaml_path, xaml, xaml_path.read_bytes().startswith(b"\xef\xbb\xbf"))
        print(f"  patched {xaml_path.relative_to(root)} (login type pills)")

    code_path = find_source(root, "PageLaunchLeft.xaml.cs", "PageChange")
    code, _ = read_source(code_path)
    code_marker = "// PCL2-CE-patch: OE login type toggle"

    if "using SvgIconControl =" not in code:
        code = code.replace(
            "using PCL.Network;",
            "using PCL.Network;\nusing SvgIconControl = PCL.Core.UI.Controls.SvgIcon.SvgIcon;",
            1,
        )

    if code_marker not in code:
        newline = "\r\n" if "\r\n" in code else "\n"
        methods = newline.join(
            [
                "    " + code_marker,
                "    private bool _syncLoginToggle;",
                "",
                "    private void BtnLoginMs_Click(object sender, MouseButtonEventArgs e)",
                "    {",
                "        if (_syncLoginToggle || !IsLoaded) return;",
                "        RefreshPage(true, ModLaunch.McLoginType.Ms);",
                "    }",
                "",
                "    private void BtnLoginOffline_Click(object sender, MouseButtonEventArgs e)",
                "    {",
                "        if (_syncLoginToggle || !IsLoaded) return;",
                "        RefreshPage(true, ModLaunch.McLoginType.Legacy);",
                "    }",
                "",
                "    private void SyncLoginToggle()",
                "    {",
                "        var isAuth = pageCurrent == PageType.Auth ||",
                "                     ProfileService.Current?.ProfileType is ProfileType.Authlib or ProfileType.YggdrasilConnect;",
                "        PanLoginToggle.Visibility = isAuth ? Visibility.Collapsed : Visibility.Visible;",
                "        PanLoginAuthType.Visibility = isAuth ? Visibility.Visible : Visibility.Collapsed;",
                "        if (isAuth)",
                "            LabLoginAuthType.Text = string.IsNullOrWhiteSpace(PageLoginAuth.ActiveServerName)",
                '                ? "第三方登录"',
                '                : PageLoginAuth.ActiveServerName + " 登录";',
                "        var ms = false;",
                "        var offline = false;",
                "        if (pageCurrent == PageType.Offline)",
                "            offline = true;",
                "        else if (pageCurrent == PageType.Ms)",
                "            ms = true;",
                "        else if (ProfileService.Current?.ProfileType == ProfileType.Microsoft)",
                "            ms = true;",
                "        else if (ProfileService.Current?.ProfileType == ProfileType.Offline)",
                "            offline = true;",
                "        else",
                "            ms = true;",
                "        SetLoginToggle(ms, offline);",
                "    }",
                "",
                "    private void SetLoginToggle(bool msSelected, bool offlineSelected)",
                "    {",
                "        _syncLoginToggle = true;",
                "        try",
                "        {",
                "            ApplyLoginPill(BtnLoginMs, IconLoginMs, LabLoginMs, msSelected);",
                "            ApplyLoginPill(BtnLoginOffline, IconLoginOffline, LabLoginOffline, offlineSelected);",
                "        }",
                "        finally",
                "        {",
                "            _syncLoginToggle = false;",
                "        }",
                "    }",
                "",
                "    private static void ApplyLoginPill(Border border, SvgIconControl icon, TextBlock text, bool selected)",
                "    {",
                "        var accent = new SolidColorBrush(Color.FromRgb(0x12, 0x7A, 0xE1));",
                "        var soft = new SolidColorBrush(Color.FromRgb(0xEE, 0xF6, 0xFD));",
                "        border.Background = selected ? accent : soft;",
                "        var foreground = selected ? Brushes.White : accent;",
                "        text.Foreground = foreground;",
                "        icon.IconBrush = foreground;",
                "    }",
                "",
            ]
        )
        anchor = "    #region 切换登录页面"
        if anchor not in code:
            raise PatchError("Could not find the login page region")
        code = code.replace(anchor, methods + anchor, 1)
        code = code.replace(
            "            pageCurrent = type;",
            "            pageCurrent = type;\n            SyncLoginToggle();",
            1,
        )
        write_source(code_path, code, code_path.read_bytes().startswith(b"\xef\xbb\xbf"))
        print(f"  patched {code_path.relative_to(root)} (login type handlers)")


def patch_oe_offline_panel(root: Path) -> None:
    """Recreate official PageLoginLegacy: skin + editable name, standard UUID."""
    path = find_source(root, "PageLoginOffline.xaml", "PageLoginOffline")
    _, newline = read_source(path)
    xaml = newline.join(
        [
            '<Grid x:Class="PCL.PageLoginOffline" Tag="False"',
            '      xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"',
            '      xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"',
            '      xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"',
            '      xmlns:d="http://schemas.microsoft.com/expression/blend/2008"',
            '      xmlns:local="clr-namespace:PCL"',
            '      mc:Ignorable="d" d:DesignWidth="297.6">',
            '    <StackPanel Margin="0,0,0,5" x:Name="PanMain">',
            '        <local:MySkin HorizontalAlignment="Center" x:Name="Skin" HasCape="False" Margin="0,0,0,15" />',
            '        <local:MyComboBox IsEditable="True" x:Name="TextName" Height="28"',
            '                          IsTextSearchEnabled="False" HintText="游戏用户名" />',
            "    </StackPanel>",
            "</Grid>",
            "",
        ]
    )
    write_source(path, xaml, path.read_bytes().startswith(b"\xef\xbb\xbf"))
    print(f"  patched {path.relative_to(root)} (official offline login)")

    code_path = find_source(root, "PageLoginOffline.xaml.cs", "PageLoginOffline")
    _, code_newline = read_source(code_path)
    code = code_newline.join(
        [
            "using System;",
            "using System.Linq;",
            "using System.Threading.Tasks;",
            "using System.Windows;",
            "using PCL.Core.App.Localization;",
            "using PCL.Core.Minecraft.Profile;",
            "using PCL.Core.Minecraft.Profile.Models;",
            "using PCL.Core.Utils.Validate;",
            "",
            "namespace PCL;",
            "",
            "public partial class PageLoginOffline",
            "{",
            "    public static string PreviewName = \"\";",
            "",
            "    public PageLoginOffline()",
            "    {",
            "        InitializeComponent();",
            "        Skin.loader = PageLaunchLeft.skinLegacy;",
            "        TextName.TextChanged += (_, _) => RefreshPreview();",
            "        Loaded += (_, _) => RefreshPreview();",
            "        foreach (var profile in ProfileService.Profiles)",
            "        {",
            "            if (profile.ProfileType == ProfileType.Offline)",
            "                TextName.Items.Add(new MyComboBoxItem { Content = profile.UserName, Tag = profile });",
            "        }",
            "    }",
            "",
            "    private async void RefreshPreview()",
            "    {",
            "        PreviewName = TextName.Text.Trim();",
            "        try",
            "        {",
            "            Skin.loader.Start();",
            "            await Task.Run(() => Skin.loader.WaitForExit());",
            "            if (IsLoaded) Skin.Load();",
            "        }",
            "        catch",
            "        {",
            "        }",
            "    }",
            "",
            "    /// <summary>Create or select the offline profile for the typed name.</summary>",
            "    public bool EnsureProfile()",
            "    {",
            "        var username = TextName.Text.Trim();",
            '        if (!new RegexValidator("^[A-z0-9_]{3,16}$").Validate(username).IsValid)',
            "        {",
            '            HintService.Hint(Lang.Text("Launch.Account.Offline.InvalidPlayerId.Message"), HintType.Error);',
            "            return false;",
            "        }",
            "",
            "        var existing = ProfileService.Profiles.FirstOrDefault(profile =>",
            "            profile.ProfileType == ProfileType.Offline &&",
            "            string.Equals(profile.UserName, username, StringComparison.OrdinalIgnoreCase));",
            "        if (existing is not null)",
            "        {",
            "            ProfileService.Select(existing);",
            "            ProfileService.IsCreatingProfile = false;",
            "            return true;",
            "        }",
            "",
            "        var uuid = ProfileUi.GetOfflineUuid(username);",
            "        var profile = new McProfile",
            "        {",
            "            ProfileType = ProfileType.Offline,",
            "            Uuid = uuid,",
            "            UserName = username,",
            '            Description = "",',
            "            AccessToken = uuid,",
            "            ClientToken = uuid,",
            '            ProfileId = Guid.NewGuid().ToString("N")',
            "        };",
            "        ProfileService.Add(profile);",
            "        ProfileService.IsCreatingProfile = false;",
            '        HintService.Hint(Lang.Text("Launch.Account.Profile.Created"), HintType.Success);',
            "        return true;",
            "    }",
            "}",
            "",
        ]
    )
    write_source(code_path, code, code_path.read_bytes().startswith(b"\xef\xbb\xbf"))
    print(f"  patched {code_path.relative_to(root)} (auto-create offline profile)")


def patch_oe_single_profile_flow(root: Path) -> None:
    """Use official-style single-account flow instead of CE's profile list."""
    path = find_source(root, "PageLaunchLeft.xaml.cs", "type = PageType.Profile;")
    code, newline = read_source(path)
    marker = "// PCL2-CE-patch: OE single-profile flow"
    if marker not in code:
        old = newline.join(
            [
                "        else",
                "        {",
                "            type = PageType.Profile;",
                "            if (_launchButtonAction != LaunchButtonAction.Download)",
                "                BtnLaunch.IsEnabled = false;",
                "        }",
            ]
        )
        replacement = newline.join(
            [
                "        else",
                "        {",
                "            " + marker,
                "            type = pageCurrent == PageType.Offline ? PageType.Offline : PageType.Ms;",
                "            if (_launchButtonAction != LaunchButtonAction.Download)",
                "                BtnLaunch.IsEnabled = type == PageType.Offline;",
                "        }",
            ]
        )
        if old not in code:
            print("  note: OE profile flow anchor not found; skipped")
            return
        code = code.replace(old, replacement, 1)
        auth_only = newline.join(
            [
                "        // PCL2-CE-patch: OE instance Auth-only login",
                "        var authOnly = false;",
                "        try",
                "        {",
                "            var instance = ModInstanceList.McMcInstanceSelected;",
                "            authOnly = instance is not null &&",
                "                       Config.InstanceAuth.LoginRequirementSolution[instance.PathInstance] == 2;",
                "        }",
                "        catch",
                "        {",
                "        }",
                "        if (authOnly && (ProfileService.Current is null ||",
                "                         ProfileService.Current.ProfileType is not ProfileType.Authlib and not ProfileType.YggdrasilConnect))",
                "        {",
                "            if (pageCurrent != PageType.Auth)",
                "                PageChange(PageType.Auth, anim);",
                "            BtnLaunch.IsEnabled = true;",
                "            return;",
                "        }",
            ]
        )
        anchor = "        var type = default(PageType);"
        if anchor in code:
            code = code.replace(anchor, auth_only + newline + anchor, 1)
        write_source(path, code, path.read_bytes().startswith(b"\xef\xbb\xbf"))
        print(f"  patched {path.relative_to(root)} (single-profile flow)")

    # Allow launching from the offline login page, and create the profile on launch.
    code, _ = read_source(path)
    enabled_old = newline.join(
        [
            "                if (ProfileService.Current is not null)",
            "                    BtnLaunch.IsEnabled = true;",
            "                else",
            "                    BtnLaunch.IsEnabled = false;",
        ]
    )
    enabled_new = newline.join(
        [
            "                if (ProfileService.Current is not null || pageCurrent == PageType.Offline ||",
            "                    pageCurrent == PageType.Auth)",
            "                    BtnLaunch.IsEnabled = true;",
            "                else",
            "                    BtnLaunch.IsEnabled = false;",
        ]
    )
    if enabled_old in code:
        code = code.replace(enabled_old, enabled_new, 1)

    launch_old = newline.join(
        [
            "            case LaunchButtonAction.Launch:",
            "            {",
            "                if (File.Exists(ModInstanceList.McMcInstanceSelected.PathInstance + \".pclignore\"))",
        ]
    )
    launch_new = newline.join(
        [
            "            case LaunchButtonAction.Launch:",
            "            {",
            "                // PCL2-CE-patch: OE auto-create offline profile",
            "                if (ProfileService.Current is null && pageCurrent == PageType.Offline)",
            "                {",
            "                    if (ModMain.frmLoginOffline is null || !ModMain.frmLoginOffline.EnsureProfile())",
            "                        return;",
            "                }",
            "                // PCL2-CE-patch: OE auth login on launch",
            "                if (pageCurrent == PageType.Auth)",
            "                {",
            "                    if (ModMain.frmLoginAuth is null || !await ModMain.frmLoginAuth.EnsureProfileAsync())",
            "                        return;",
            "                }",
            "                if (File.Exists(ModInstanceList.McMcInstanceSelected.PathInstance + \".pclignore\"))",
        ]
    )
    if launch_old in code:
        code = code.replace(launch_old, launch_new, 1)
    code = code.replace("    public void LaunchButtonClick()", "    public async void LaunchButtonClick()", 1)
    write_source(path, code, path.read_bytes().startswith(b"\xef\xbb\xbf"))

    skin_path = find_source(root, "PageLoginProfileSkin.xaml.cs", "private void ChangeProfile")
    skin, _ = read_source(skin_path)
    marker = "// PCL2-CE-patch: OE switch account"
    if marker not in skin:
        old = newline.join(
            [
                "    private void ChangeProfile(object sender, EventArgs e)",
                "    {",
                "        ProfileService.Select(null);",
                "        ModBase.RunInUi(() =>",
                "        {",
                "            ModMain.frmLaunchLeft.RefreshPage(true);",
                "            ModMain.frmLaunchLeft.BtnLaunch.IsEnabled = false;",
                "        });",
                "    }",
            ]
        )
        replacement = newline.join(
            [
                "    private void ChangeProfile(object sender, EventArgs e)",
                "    {",
                "        " + marker,
                "        var current = ProfileService.Current;",
                "        var wasOffline = current?.ProfileType == ProfileType.Offline;",
                "        var wasAuth = current?.ProfileType is ProfileType.Authlib or ProfileType.YggdrasilConnect;",
                "        var authServer = current?.Server;",
                "        ProfileService.Select(null);",
                "        if (wasAuth) PageLoginAuth.draggedAuthServer = authServer;",
                "        ModBase.RunInUi(() =>",
                "        {",
                "            ModMain.frmLaunchLeft.RefreshPage(true, wasAuth",
                "                ? ModLaunch.McLoginType.Auth",
                "                : wasOffline ? ModLaunch.McLoginType.Legacy : ModLaunch.McLoginType.Ms);",
                "            ModMain.frmLaunchLeft.BtnLaunch.IsEnabled = false;",
                "        });",
                "    }",
            ]
        )
        if old not in skin:
            print("  note: OE switch-account anchor not found; skipped")
            return
        skin = skin.replace(old, replacement, 1)
        write_source(skin_path, skin, skin_path.read_bytes().startswith(b"\xef\xbb\xbf"))
        print(f"  patched {skin_path.relative_to(root)} (switch account)")


def patch_oe_offline_preview(root: Path) -> None:
    """Feed the typed offline name to the Steve/Alex skin preview loader."""
    path = find_source(root, "PageLaunchLeft.xaml.cs", "SkinLegacyInput")
    code, newline = read_source(path)
    marker = "// PCL2-CE-patch: OE offline preview"
    if marker in code:
        return
    old = newline.join(
        [
            "    private static ModBase.EqualableList<string> SkinLegacyInput()",
            "    {",
            "        return new ModBase.EqualableList<string>",
            "            { ProfileService.Current?.UserName ?? \"\", ProfileService.Current?.Uuid ?? \"\" };",
            "    }",
        ]
    )
    replacement = newline.join(
        [
            "    private static ModBase.EqualableList<string> SkinLegacyInput()",
            "    {",
            "        " + marker,
            "        if (ProfileService.Current?.ProfileType != ProfileType.Offline &&",
            "            !string.IsNullOrWhiteSpace(PageLoginOffline.PreviewName))",
            "        {",
            "            var name = PageLoginOffline.PreviewName;",
            "            return new ModBase.EqualableList<string> { name, ProfileUi.GetOfflineUuid(name) };",
            "        }",
            "        return new ModBase.EqualableList<string>",
            "            { ProfileService.Current?.UserName ?? \"\", ProfileService.Current?.Uuid ?? \"\" };",
            "    }",
        ]
    )
    if old not in code:
        print("  note: offline skin input anchor not found; skipped")
        return
    code = code.replace(old, replacement, 1)
    write_source(path, code, path.read_bytes().startswith(b"\xef\xbb\xbf"))
    print(f"  patched {path.relative_to(root)} (offline skin preview)")


def patch_oe_login_panel(root: Path) -> None:
    """Match the official Microsoft login row: account combo + 登录 button."""
    path = find_source(root, "PageLoginMs.xaml", "BtnLogin")
    text, newline = read_source(path)
    marker = "<!-- PCL2-CE-patch: OE login row -->"
    if marker in text:
        return

    old = newline.join(
        [
            '        <local:MyButton Text="{DynamicResource Launch.Account.Microsoft.Start}" x:Name="BtnLogin" ColorType="Highlight"',
            '                        Height="28" Width="100" Margin="0,0,0,0" Grid.Row="2" Grid.Column="0" />',
        ]
    )
    replacement = newline.join(
        [
            "        " + marker,
            '        <Grid Grid.Row="2" Grid.ColumnSpan="2">',
            "            <Grid.ColumnDefinitions>",
            "                <ColumnDefinition />",
            "                <ColumnDefinition Width=\"Auto\" />",
            "            </Grid.ColumnDefinitions>",
            '            <local:MyComboBox x:Name="ComboAccounts" Height="28" Margin="0,0,0,0">',
            "                <local:MyComboBox.ContextMenu>",
            "                    <ContextMenu>",
            '                        <local:MyMenuItem Header="删除当前账号" Click="DeleteAccount_Click" />',
            "                    </ContextMenu>",
            "                </local:MyComboBox.ContextMenu>",
            '                <local:MyComboBoxItem Content="添加新账号" />',
            "            </local:MyComboBox>",
            '            <local:MyButton x:Name="BtnLogin" Grid.Column="1" Height="28" Width="60"',
            '                            Margin="10,0,0,0" ColorType="Highlight"',
            '                            Text="{DynamicResource Launch.Account.Login}" />',
            "        </Grid>",
        ]
    )
    if old not in text:
        print("  note: official login row anchor not found; skipped")
        return
    text = text.replace(old, replacement, 1)
    text = text.replace(
        '<local:MyTextButton x:Name="BtnBack"',
        '<local:MyTextButton x:Name="BtnBack" Visibility="Collapsed"',
        1,
    )
    write_source(path, text, path.read_bytes().startswith(b"\xef\xbb\xbf"))
    print(f"  patched {path.relative_to(root)} (official login row)")

    code_path = find_source(root, "PageLoginMs.xaml.cs", "InitializeComponent")
    code, _ = read_source(code_path)
    code_marker = "// PCL2-CE-patch: OE login accounts"
    if code_marker not in code:
        if "using System.Windows.Controls;" not in code:
            code = code.replace(
                "using System.Windows;",
                "using System.Windows;\nusing System.Windows.Controls;",
                1,
            )
        if "using PCL.Core.Minecraft.Profile.Models;" not in code:
            code = code.replace(
                "using PCL.Core.Minecraft.Profile;",
                "using PCL.Core.Minecraft.Profile;\nusing PCL.Core.Minecraft.Profile.Models;",
                1,
            )
        file_newline = "\r\n" if "\r\n" in code else "\n"
        insert = file_newline.join(
            [
                "        " + code_marker,
                "        foreach (var profile in ProfileService.Profiles)",
                "        {",
                "            if (profile.ProfileType == ProfileType.Microsoft)",
                "                ComboAccounts.Items.Add(new MyComboBoxItem { Content = profile.UserName, Tag = profile });",
                "        }",
                "        ComboAccounts.SelectedIndex = 0;",
            ]
        )
        code = code.replace(
            "        InitializeComponent();",
            "        InitializeComponent();" + file_newline + insert,
            1,
        )
        login_old = file_newline.join(
            [
                "    private void BtnLogin_Click(object sender, EventArgs e)",
                "    {",
                "        BtnLogin.IsEnabled = false;",
            ]
        )
        login_new = file_newline.join(
            [
                "    private void BtnLogin_Click(object sender, EventArgs e)",
                "    {",
                "        // PCL2-CE-patch: OE existing account login",
                "        if (ComboAccounts.SelectedItem is MyComboBoxItem { Tag: McProfile existing })",
                "        {",
                "            ProfileService.Select(existing);",
                "            ProfileService.IsCreatingProfile = false;",
                "            ModBase.RunInUi(() => ModMain.frmLaunchLeft.RefreshPage(true));",
                "            return;",
                "        }",
                "        BtnLogin.IsEnabled = false;",
            ]
        )
        if login_old not in code:
            print("  note: login click anchor not found; skipped")
            return
        code = code.replace(login_old, login_new, 1)
        code = code.replace(
            "                    BtnBack.Visibility = Visibility.Visible;",
            "                    BtnBack.Visibility = Visibility.Collapsed;",
            1,
        )
        delete_handler = file_newline.join(
            [
                "    // PCL2-CE-patch: OE delete account",
                "    private void DeleteAccount_Click(object sender, RoutedEventArgs e)",
                "    {",
                "        if (ComboAccounts.SelectedItem is not MyComboBoxItem { Tag: McProfile profile }) return;",
                "        ProfileService.Remove(profile);",
                "        ComboAccounts.Items.Remove(ComboAccounts.SelectedItem);",
                "        if (ProfileService.Current == profile)",
                "            ProfileService.Select(null);",
                "        ComboAccounts.SelectedIndex = 0;",
                "        ModMain.frmLaunchLeft.RefreshPage(true, ModLaunch.McLoginType.Ms);",
                "    }",
                "",
            ]
        )
        code = code.replace(
            "    private void BtnBack_Click",
            delete_handler + "    private void BtnBack_Click",
            1,
        )
        write_source(code_path, code, code_path.read_bytes().startswith(b"\xef\xbb\xbf"))
        print(f"  patched {code_path.relative_to(root)} (login account list)")


def patch_oe_settings_order(root: Path) -> None:
    """Collapse OE settings into 启动 / 个性化 / 其他 like official PCL."""
    path = find_source(root, "PageSetupLeft.xaml", "TextAboutCategory")
    text, newline = read_source(path)
    marker = "<!-- PCL2-CE-patch: OE settings order -->"
    if marker in text:
        return

    game = re.search(
        r"^[ \t]*<local:MyListItem x:Name=\"ItemGameLink\".*?</local:MyListItem>[ \t]*\r?\n",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if game is None:
        print("  note: settings GameLink item not found; skipped")
        return
    game_link = game.group(0).rstrip("\r\n")
    text = text[: game.start()] + text[game.end() :]

    # Remove the old “工具” category header and its trailing blank line.
    text = re.sub(
        r"^[ \t]*<TextBlock[^\n]*Setup\.Left\.Category\.Tools[^\n]*\r?\n(?:[ \t]*\r?\n)?",
        "",
        text,
        count=1,
        flags=re.MULTILINE,
    )
    # Collapse the extra blank line left before “启动器/个性化”.
    text = re.sub(
        r"(?:\r?\n){2,}(?=[ \t]*<TextBlock[^\n]*Category\.Launcher)",
        newline,
        text,
        count=1,
    )

    # Put the link entry under the “其他” category.
    about = re.search(
        r"^[ \t]*<TextBlock[^\n]*Category\.About[^\n]*\r?\n",
        text,
        re.MULTILINE,
    )
    if about is None:
        raise PatchError("Could not find the settings About category")
    insert = newline + "            " + marker + newline + game_link + newline
    text = text[: about.end()] + insert + text[about.end() :]
    write_source(path, text, path.read_bytes().startswith(b"\xef\xbb\xbf"))
    print(f"  patched {path.relative_to(root)} (settings 3-category order)")

    # The category header no longer exists; drop its visibility tuple.
    ui_path = find_source(root, "PageSetupUI.xaml.cs", "TextToolsCategory")
    ui_text, _ = read_source(ui_path)
    ui_text, count = re.subn(
        r"^[ \t]*\(ModMain\.frmSetupLeft\.TextToolsCategory,[^\n]*\r?\n",
        "",
        ui_text,
        count=1,
        flags=re.MULTILINE,
    )
    if count == 1:
        write_source(ui_path, ui_text, ui_path.read_bytes().startswith(b"\xef\xbb\xbf"))
        print(f"  patched {ui_path.relative_to(root)} (removed Tools category visibility)")


def patch_oe_more_links(root: Path) -> None:
    """Add the official Help / Vote entries to the More page."""
    path = find_source(root, "PageToolsLeft.xaml", "ItemFeedback")
    xaml, newline = read_source(path)
    marker = "<!-- PCL2-CE-patch: OE more links -->"
    if marker not in xaml:
        anchor = (
            '        <local:MyListItem x:Name="ItemFeedback" IsScaleAnimationEnabled="False" Type="RadioBox"'
        )
        block = newline.join(
            [
                "        " + marker,
                '        <local:MyListItem x:Name="ItemHelp" IsScaleAnimationEnabled="False" Type="Clickable"',
                '                           MinPaddingRight="35" Height="36"',
                '                           Title="帮助" SvgIcon="lucide/circle-help"',
                '                           Tag="https://github.com/LTCatt/PCL2Help" Click="OpenMoreLink" />',
                '        <local:MyListItem x:Name="ItemVote" IsScaleAnimationEnabled="False" Type="Clickable"',
                '                           MinPaddingRight="35" Height="36"',
                '                           Title="新功能投票" SvgIcon="lucide/layout-list"',
                '                           Tag="https://github.com/Meloong-Git/PCL/discussions/2" Click="OpenMoreLink" />',
            ]
        )
        if anchor not in xaml:
            print("  note: More link anchor not found; skipped")
            return
        xaml = xaml.replace(anchor, block + newline + anchor, 1)
        write_source(path, xaml, path.read_bytes().startswith(b"\xef\xbb\xbf"))
        print(f"  patched {path.relative_to(root)} (Help / Vote links)")

    code_path = find_source(root, "PageToolsLeft.xaml.cs", "PageCheck")
    code, _ = read_source(code_path)
    code_marker = "// PCL2-CE-patch: OE more links"
    if code_marker not in code:
        if "using System.Windows.Input;" not in code:
            code = code.replace("using System.Windows;", "using System.Windows;\nusing System.Windows.Input;", 1)
        file_newline = "\r\n" if "\r\n" in code else "\n"
        method = file_newline.join(
            [
                "    " + code_marker,
                "    private void OpenMoreLink(object sender, MouseButtonEventArgs e)",
                "    {",
                "        if (sender is MyListItem { Tag: string url })",
                "            ModBase.OpenWebsite(url);",
                "    }",
                "",
            ]
        )
        anchor = "    #region 页面切换"
        if anchor not in code:
            raise PatchError("Could not find PageTools page region")
        code = code.replace(anchor, method + anchor, 1)
        write_source(code_path, code, code_path.read_bytes().startswith(b"\xef\xbb\xbf"))
        print(f"  patched {code_path.relative_to(root)} (Help / Vote handlers)")


def patch_oe_auth_page(root: Path) -> None:
    """Recreate official PageLoginAuth: email/password/remember/register."""
    path = find_source(root, "PageLoginAuth.xaml", "PageLoginAuth")
    _, newline = read_source(path)
    xaml = newline.join(
        [
            '<Grid x:Class="PCL.PageLoginAuth" Tag="False"',
            '      xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"',
            '      xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"',
            '      xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"',
            '      xmlns:d="http://schemas.microsoft.com/expression/blend/2008"',
            '      xmlns:local="clr-namespace:PCL"',
            '      mc:Ignorable="d" d:DesignWidth="317.6" Margin="0,0,0,-2">',
            "    <Grid.RowDefinitions>",
            '        <RowDefinition Height="Auto" />',
            '        <RowDefinition Height="10" />',
            '        <RowDefinition Height="Auto" />',
            '        <RowDefinition Height="Auto" />',
            "    </Grid.RowDefinitions>",
            "    <Grid.ColumnDefinitions>",
            "        <ColumnDefinition />",
            '        <ColumnDefinition Width="224" />',
            "    </Grid.ColumnDefinitions>",
            '    <local:MyComboBox Grid.Row="0" Grid.Column="1" IsEditable="True" Height="28" x:Name="TextName" />',
            '    <TextBlock x:Name="TextNameTitle" Text="邮箱" Grid.Row="0" VerticalAlignment="Center" />',
            '    <PasswordBox Grid.Row="2" Grid.Column="1" x:Name="TextPass" />',
            '    <TextBlock x:Name="TextPassTitle" Text="密码" Grid.Row="2" VerticalAlignment="Center" />',
            '    <local:MyCheckBox x:Name="CheckRemember" Tag="LoginRemember" Height="20" Margin="-1,12,80,0"',
            '                     Text="记住密码" Grid.Row="3" Grid.Column="1" Grid.ColumnSpan="2" />',
            '    <local:MyTextButton Margin="0,12,2,0" x:Name="BtnLink" Content="注册账号" Grid.Row="3"',
            '                        HorizontalAlignment="Right" Grid.Column="1" Padding="20,0,0,0"',
            '                        HorizontalContentAlignment="Right" FontSize="13" VerticalContentAlignment="Center" />',
            "    <Grid x:Name=\"PanHidden\" Grid.RowSpan=\"4\" Grid.ColumnSpan=\"2\" Visibility=\"Collapsed\">",
            '        <Grid x:Name="PanOAuth">',
            '            <local:MyButton x:Name="BtnOAuth" Text="OAuth 登录" />',
            '            <local:MyButton x:Name="BtnBackOAuth" Text="返回" />',
            '            <local:MyTextButton x:Name="BtnUsePassword" Text="使用密码" />',
            '            <local:MyTextButton x:Name="BtnUseOAuth" Text="使用 OAuth" />',
            '            <local:MyTextButton x:Name="BtnWebsite" Text="官网" />',
            '            <local:MyTextButton x:Name="BtnPasswordWebsite" Text="官网" />',
            '            <Grid x:Name="PanPasswordLinks" />',
            "        </Grid>",
            '        <local:MyButton x:Name="BtnLogin" Text="登录" />',
            '        <local:MyButton x:Name="BtnBack" Text="返回" />',
            "    </Grid>",
            "</Grid>",
            "",
        ]
    )
    write_source(path, xaml, path.read_bytes().startswith(b"\xef\xbb\xbf"))
    print(f"  patched {path.relative_to(root)} (official Auth form)")

    code_path = find_source(root, "PageLoginAuth.xaml.cs", "PageLoginAuth")
    code, _ = read_source(code_path)
    marker = "// PCL2-CE-patch: OE auth page"
    if marker in code:
        return
    # Force the simple password mode and keep every OAuth/back control hidden.
    old_reload = (
        "    private void Reload()\n"
        "    {\n"
        '        _authServerUrl = draggedAuthServer ?? "";\n'
        "        var knownOAuthSupport = draggedAuthServerOAuthSupported;\n"
        "        draggedAuthServer = null;\n"
        "        draggedAuthServerOAuthSupported = null;\n"
        "        _oauthSupported = knownOAuthSupport;\n"
        "        _UpdateOAuthEntryVisibility();\n"
        "        _SetLoginMode(knownOAuthSupport == true);\n"
    )
    new_reload = (
        "    private void Reload()\n"
        "    {\n"
        "        // PCL2-CE-patch: OE auth page\n"
        '        _authServerUrl = draggedAuthServer ?? "";\n'
        '        ActiveServerName = _authServerUrl.Contains("littleskin", StringComparison.OrdinalIgnoreCase) ? "LittleSkin" : "第三方登录";\n'
        '        if (ModMain.frmLaunchLeft is not null) ModMain.frmLaunchLeft.LabLoginAuthType.Text = ActiveServerName + " 登录";\n'
        "        draggedAuthServer = null;\n"
        "        draggedAuthServerOAuthSupported = null;\n"
        "        _oauthSupported = false;\n"
        "        _SetLoginMode(false);\n"
        "        BtnLogin.Visibility = Visibility.Collapsed;\n"
        "        BtnBack.Visibility = Visibility.Collapsed;\n"
        "        BtnOAuth.Visibility = Visibility.Collapsed;\n"
        "        BtnBackOAuth.Visibility = Visibility.Collapsed;\n"
        "        BtnUsePassword.Visibility = Visibility.Collapsed;\n"
        "        BtnUseOAuth.Visibility = Visibility.Collapsed;\n"
        "        BtnWebsite.Visibility = Visibility.Collapsed;\n"
        "        BtnPasswordWebsite.Visibility = Visibility.Collapsed;\n"
        "        PanOAuth.Visibility = Visibility.Collapsed;\n"
        "        PanPasswordLinks.Visibility = Visibility.Collapsed;\n"
        "        if (ModMain.frmLaunchLeft is not null) ModMain.frmLaunchLeft.BtnLaunch.IsEnabled = true;\n"
    )
    old_reload = old_reload.replace("\n", newline)
    new_reload = new_reload.replace("\n", newline)
    if old_reload not in code:
        print("  note: Auth Reload anchor not found; skipped code patch")
        return
    code = code.replace(old_reload, new_reload, 1)
    old_tail = newline.join(
        [
            "        if (knownOAuthSupport is null && !string.IsNullOrWhiteSpace(_authServerUrl))",
            "        {",
            "            var server = _authServerUrl;",
            "            Dispatcher.BeginInvoke(new Func<Task>(async () =>",
            "            {",
            "                var supported = await IsOAuthSupportedAsync(server).ConfigureAwait(true);",
            "                if (!string.Equals(_authServerUrl, server, StringComparison.OrdinalIgnoreCase))",
            "                    return;",
            "                _oauthSupported = supported;",
            "                _UpdateOAuthEntryVisibility();",
            "                _SetLoginMode(supported);",
            "            }));",
            "        }",
        ]
    )
    code = code.replace(old_tail, "", 1)
    code = code.replace(
        "        var address = Config.InstanceAuth.AuthRegisterAddress.ToString();",
        "        _isRegisterMode = false;" + newline
        + "        var address = Config.InstanceAuth.AuthRegisterAddress.ToString();",
        1,
    )
    code = code.replace(
        "        _hasRegisterLink = new HttpValidator().Validate(address).IsValid;",
        "        _hasRegisterLink = new HttpValidator().Validate(address).IsValid ||"
        + newline + "                          !string.IsNullOrWhiteSpace(_authServerUrl);",
        1,
    )
    old_click = newline.join(
        [
            "    private void Btn_Click(object sender, EventArgs e)",
            "    {",
            "        ModBase.OpenWebsite(_isRegisterMode",
            '            ? Config.InstanceAuth.AuthRegisterAddress.ToString()',
            '            : Config.InstanceAuth.AuthRegisterAddress.ToString().Replace("/auth/register", "/auth/forgot"));',
            "    }",
        ]
    )
    new_click = newline.join(
        [
            "    private void Btn_Click(object sender, EventArgs e)",
            "    {",
            "        var address = Config.InstanceAuth.AuthRegisterAddress.ToString();",
            "        if (_isRegisterMode)",
            '            address = address.Replace("/auth/forgot", "/auth/register");',
            "        else",
            '            address = address.Replace("/auth/register", "/auth/forgot");',
            "        if (string.IsNullOrWhiteSpace(address) && !string.IsNullOrWhiteSpace(_authServerUrl))",
            '            address = _authServerUrl.Replace("/api/yggdrasil", _isRegisterMode ? "/auth/register" : "/auth/forgot");',
            "        if (!string.IsNullOrWhiteSpace(address))",
            "            ModBase.OpenWebsite(address);",
            "    }",
        ]
    )
    code = code.replace(old_click, new_click, 1)
    class_marker = "public partial class PageLoginAuth" + newline + "{"
    code = code.replace(
        class_marker,
        class_marker + newline + "    public static string ActiveServerName = \"\";",
        1,
    )
    code = code.replace(
        "                BtnBack.Visibility = Visibility.Visible;",
        "                BtnBack.Visibility = Visibility.Collapsed;",
        1,
    )
    # Add the launch-time login entry point.
    ensure = newline.join(
        [
            "    // PCL2-CE-patch: OE auth ensure profile",
            "    public async Task<bool> EnsureProfileAsync()",
            "    {",
            "        if (string.IsNullOrWhiteSpace(_authServerUrl))",
            "            _authServerUrl = draggedAuthServer ?? \"\";",
            "        if (string.IsNullOrWhiteSpace(_authServerUrl) && PageInstanceLeft.McInstance is not null)",
            "            _authServerUrl = Config.InstanceAuth.AuthServerAddress[PageInstanceLeft.McInstance.PathInstance];",
            "        if (string.IsNullOrWhiteSpace(_authServerUrl))",
            "        {",
            '            HintService.Hint(Lang.Text("Launch.Account.Auth.EmptyFields"), HintType.Error);',
            "            return false;",
            "        }",
            "        if (string.IsNullOrWhiteSpace(TextName.Text) || string.IsNullOrWhiteSpace(TextPass.Password))",
            "        {",
            '            HintService.Hint(Lang.Text("Launch.Account.Auth.EmptyFields"), HintType.Error);',
            "            return false;",
            "        }",
            "        var loginData = new ModLaunch.McLoginServer(ModLaunch.McLoginType.Auth)",
            "        {",
            "            BaseUrl = await ApiLocation.TryRequestAsync(_authServerUrl).ConfigureAwait(true),",
            '            UserName = TextName.Text, Password = TextPass.Password, Description = "Authlib-Injector",',
            "            LoginType = ModLaunch.McLoginType.Auth",
            "        };",
            "        ProfileService.IsCreatingProfile = true;",
            "        ModLaunch.mcLoginAuthLoader.Start(loginData, true);",
            "        while (ModLaunch.mcLoginAuthLoader.State == ModBase.LoadState.Loading)",
            "            await Task.Delay(50);",
            "        if (ModLaunch.mcLoginAuthLoader.State == ModBase.LoadState.Finished)",
            "        {",
            "            ModMain.frmLaunchLeft.RefreshPage(true);",
            "            return true;",
            "        }",
            "        ProfileService.IsCreatingProfile = false;",
            "        return false;",
            "    }",
            "",
        ]
    )
    code = code.replace("    private void BtnBack_Click", ensure + "    private void BtnBack_Click", 1)
    write_source(code_path, code, code_path.read_bytes().startswith(b"\xef\xbb\xbf"))
    print(f"  patched {code_path.relative_to(root)} (official Auth behavior)")


def patch_oe_auth_entry(root: Path) -> None:
    """Expose the existing per-version Authlib entry as 第三方登录."""
    path = find_source(root, "PageInstanceSetup.xaml", "BtnServerNewProfile")
    text, _ = read_source(path)
    marker = "<!-- PCL2-CE-patch: OE auth entry -->"
    if marker in text:
        return
    old = 'Text="{DynamicResource Instance.Setup.Server.CreateProfile}"'
    if old not in text:
        print("  note: Auth entry button text not found; skipped")
        return
    text = text.replace(old, 'Text="第三方登录"', 1)
    text = text.replace(
        '<local:MyButton x:Name="BtnServerNewProfile"',
        marker + "\n                    <local:MyButton x:Name=\"BtnServerNewProfile\"",
        1,
    )
    write_source(path, text, path.read_bytes().startswith(b"\xef\xbb\xbf"))
    print(f"  patched {path.relative_to(root)} (Authlib entry)")


def patch_oe_more_default(root: Path) -> None:
    """Open the About page by default instead of the CE lobby."""
    path = find_source(root, "PageToolsLeft.xaml.cs", "PageLinkLeft_Loaded")
    code, newline = read_source(path)
    marker = "// PCL2-CE-patch: OE more default"
    if marker in code:
        return
    old = newline.join(
        [
            "        if (!hideCfg.ToolsGameLink)",
            "            ItemGameLink.SetChecked(true, false, false);",
            "        else if (!hideCfg.ToolsTest)",
            "            ItemTest.SetChecked(true, false, false);",
            "        else",
            "            ItemGameLink.SetChecked(true, false, false);",
        ]
    )
    replacement = newline.join(
        [
            "        " + marker,
            "        if (!hideCfg.SetupAbout)",
            "            ItemAbout.SetChecked(true, false, false);",
            "        else if (!hideCfg.ToolsTest)",
            "            ItemTest.SetChecked(true, false, false);",
            "        else",
            "            ItemGameLink.SetChecked(true, false, false);",
        ]
    )
    if old not in code:
        print("  note: More default block not found; skipped")
        return
    code = code.replace(old, replacement, 1)
    write_source(path, code, path.read_bytes().startswith(b"\xef\xbb\xbf"))
    print(f"  patched {path.relative_to(root)} (More default About)")


def patch_oe_about_actions(root: Path) -> None:
    """Match the official About card's 检查更新 action."""
    path = find_source(root, "PageSetupAbout.xaml", "Setup.About.SourceCode")
    xaml, newline = read_source(path)
    marker = "<!-- PCL2-CE-patch: OE about update -->"
    if marker not in xaml:
        source_button = re.search(
            r'[ \t]*<local:MyButton\s+Grid\.Row="2"\s+Grid\.Column="2".*?Setup\.About\.SourceCode.*?/>',
            xaml,
            re.DOTALL,
        )
        if source_button is None:
            print("  note: About source button not found; skipped")
            return
        replacement = newline.join(
            [
                "                    " + marker,
                '                    <local:MyButton Grid.Row="2" Grid.Column="2" Height="35"',
                '                                    Text="检查更新" Click="BtnCheckUpdate_Click" />',
            ]
        )
        xaml = xaml[: source_button.start()] + replacement + xaml[source_button.end() :]
        write_source(path, xaml, path.read_bytes().startswith(b"\xef\xbb\xbf"))
        print(f"  patched {path.relative_to(root)} (About update button)")

    code_path = find_source(root, "PageSetupAbout.xaml.cs", "ImgPCLLogo_Click")
    code, _ = read_source(code_path)
    code_marker = "// PCL2-CE-patch: OE about update"
    if code_marker not in code:
        file_newline = "\r\n" if "\r\n" in code else "\n"
        method = file_newline.join(
            [
                "    " + code_marker,
                "    private void BtnCheckUpdate_Click(object sender, MouseButtonEventArgs e)",
                "    {",
                "        ModMain.frmMain.PageChange(FormMain.PageType.Setup, FormMain.PageSubType.SetupUpdate);",
                "    }",
                "",
            ]
        )
        code = code.replace("    private void ImgPCLLogo_Click", method + "    private void ImgPCLLogo_Click", 1)
        write_source(code_path, code, code_path.read_bytes().startswith(b"\xef\xbb\xbf"))
        print(f"  patched {code_path.relative_to(root)} (About update handler)")


def patch_oe_hide_font_card(root: Path) -> None:
    """Hide the CE-only font card so 个性化 matches official PCL."""
    path = find_source(root, "PageSetupUI.xaml", "CardFont")
    text, _ = read_source(path)
    marker = "<!-- PCL2-CE-patch: OE hide font card -->"
    if marker in text:
        return
    text = text.replace(
        '<local:MyCard x:Name="CardFont"',
        "        " + marker + '\n        <local:MyCard x:Name="CardFont" Visibility="Collapsed"',
        1,
    )
    write_source(path, text, path.read_bytes().startswith(b"\xef\xbb\xbf"))
    print(f"  patched {path.relative_to(root)} (hidden font card)")


def patch_oe_more_flat(root: Path) -> None:
    """Remove the old category headers from the More page."""
    path = find_source(root, "PageToolsLeft.xaml", "ItemGameLink")
    text, _ = read_source(path)
    marker = "<!-- PCL2-CE-patch: OE more flat -->"
    if marker in text:
        return
    for needle in ("Tools.Left.Multiplayer", "Tools.Left.Utilities", 'Text="关于与反馈"'):
        text, count = re.subn(
            rf"[ \t]*<TextBlock[^>]*{re.escape(needle)}[^>]*/>\r?\n(?:[ \t]*\r?\n)?",
            "",
            text,
            count=1,
        )
        if count == 0:
            print(f"  note: More header {needle!r} not found; skipped")
    text = text.replace(
        '<StackPanel Orientation="Vertical" x:Name="PanItem" Margin="0,12,0,0">',
        '<StackPanel Orientation="Vertical" x:Name="PanItem" Margin="0,12,0,0">\n        ' + marker,
        1,
    )
    write_source(path, text, path.read_bytes().startswith(b"\xef\xbb\xbf"))
    print(f"  patched {path.relative_to(root)} (flat More list)")

    # Remove the old category-header visibility code in PageSetupUI.
    ui_path = find_source(root, "PageSetupUI.xaml.cs", "TextGameLinkCategory")
    ui_text, _ = read_source(ui_path)
    ui_text, count = re.subn(
        r"[ \t]*// 处理分类标题\r?\n"
        r".*?if \(isToolsVisible\) ModMain\.frmToolsLeft\.TextToolsCategory\.Opacity = 0\.6;\r?\n",
        "",
        ui_text,
        count=1,
        flags=re.DOTALL,
    )
    if count == 1:
        write_source(ui_path, ui_text, ui_path.read_bytes().startswith(b"\xef\xbb\xbf"))
        print(f"  patched {ui_path.relative_to(root)} (removed More header code)")


def patch_oe_more_order(root: Path) -> None:
    """Reorder the flat More list to the official Help/About/... sequence."""
    path = find_source(root, "PageToolsLeft.xaml", "ItemGameLink")
    text, newline = read_source(path)
    marker = "<!-- PCL2-CE-patch: OE more order -->"
    if marker in text:
        return

    stack = text.find("<StackPanel")
    open_end = text.find(">", stack) + 1
    close = text.rindex("</StackPanel>")
    inner = text[open_end:close]

    patterns = {
        "ItemGameLink": r"<local:MyListItem x:Name=\"ItemGameLink\".*?</local:MyListItem>",
        "ItemTest": r"<local:MyListItem x:Name=\"ItemTest\".*?/>",
        "ItemAbout": r"<local:MyListItem x:Name=\"ItemAbout\".*?/>",
        "ItemFeedback": r"<local:MyListItem x:Name=\"ItemFeedback\".*?/>",
        "ItemHelp": r"<local:MyListItem x:Name=\"ItemHelp\".*?/>",
        "ItemVote": r"<local:MyListItem x:Name=\"ItemVote\".*?/>",
    }
    blocks = {}
    for name, pattern in patterns.items():
        match = re.search(pattern, inner, re.DOTALL)
        if match is None:
            print(f"  note: More item {name} not found; order unchanged")
            return
        blocks[name] = match.group(0).strip()

    order = ["ItemHelp", "ItemAbout", "ItemTest", "ItemFeedback", "ItemVote", "ItemGameLink"]
    lines = ["        " + marker]
    for name in order:
        lines.append("        " + blocks[name])
    new_inner = newline + newline.join(lines) + newline
    text = text[:open_end] + new_inner + text[close:]
    write_source(path, text, path.read_bytes().startswith(b"\xef\xbb\xbf"))
    print(f"  patched {path.relative_to(root)} (More order)")


def patch_oe_more_pages(root: Path) -> None:
    """Move About/Feedback into the More (Tools) page for OE."""
    xaml_path = find_source(root, "PageToolsLeft.xaml", "ItemTest")
    xaml, _ = read_source(xaml_path)
    marker = "<!-- PCL2-CE-patch: OE more pages -->"
    if marker not in xaml:
        # The file may use CRLF; compute its newline explicitly.
        file_newline = "\r\n" if "\r\n" in xaml else "\n"
        anchor = f"    </StackPanel>{file_newline}    </local:MyPageLeft>"
        if anchor not in xaml:
            anchor = f"    </StackPanel>{file_newline}</local:MyPageLeft>"
        block = file_newline.join(
            [
                "        " + marker,
                '        <TextBlock Text="关于与反馈" Margin="13,5,5,3" Opacity="0.6" FontSize="12" />',
                '        <local:MyListItem x:Name="ItemAbout" IsScaleAnimationEnabled="False" Type="RadioBox"',
                '                           Tag="4" Check="PageCheck" MinPaddingRight="35" Height="36"',
                '                           Title="{DynamicResource Setup.Left.Item.About}"',
                '                           SvgIcon="lucide/info" />',
                '        <local:MyListItem x:Name="ItemFeedback" IsScaleAnimationEnabled="False" Type="RadioBox"',
                '                           Tag="6" Check="PageCheck" MinPaddingRight="35" Height="36"',
                '                           Title="{DynamicResource Setup.Left.Item.Feedback}"',
                '                           SvgIcon="lucide/message-circle" />',
                "",
            ]
        )
        if anchor not in xaml:
            raise PatchError("Could not find the OE More page anchor")
        xaml = xaml.replace(anchor, block + anchor, 1)
        write_source(xaml_path, xaml, xaml_path.read_bytes().startswith(b"\xef\xbb\xbf"))
        print(f"  patched {xaml_path.relative_to(root)} (About/Feedback in More)")

    code_path = find_source(root, "PageToolsLeft.xaml.cs", "PageGet")
    code, _ = read_source(code_path)
    code_marker = "// PCL2-CE-patch: OE more pages"
    if code_marker not in code:
        file_newline = "\r\n" if "\r\n" in code else "\n"
        cases = file_newline.join(
            [
                "            " + code_marker,
                "            case FormMain.PageSubType.SetupAbout:",
                "            {",
                "                ModMain.frmSetupAbout ??= new PageSetupAbout();",
                "                return ModMain.frmSetupAbout;",
                "            }",
                "            case FormMain.PageSubType.SetupFeedback:",
                "            {",
                "                ModMain.frmSetupFeedback ??= new PageSetupFeedback();",
                "                return ModMain.frmSetupFeedback;",
                "            }",
                "",
            ]
        )
        anchor = "            default:"
        if anchor not in code:
            raise PatchError("Could not find the PageTools PageGet default case")
        code = code.replace(anchor, cases + anchor, 1)
        write_source(code_path, code, code_path.read_bytes().startswith(b"\xef\xbb\xbf"))
        print(f"  patched {code_path.relative_to(root)} (About/Feedback routing)")


def patch_oe_about_footer(root: Path) -> None:
    try:
        path = find_source(root, "PageSetupAbout.xaml", "MyScrollViewer")
    except PatchError:
        return
    text, newline = read_source(path)
    marker = "<!-- PCL2-CE-patch: OE about footer -->"
    if marker in text:
        return
    anchor = f"        </StackPanel>{newline}    </local:MyScrollViewer>"
    if anchor not in text:
        print("  note: About page footer anchor not found; skipped")
        return
    footer = newline.join(
        [
            "            " + marker,
            '            <TextBlock Margin="21,0,21,20" HorizontalAlignment="Center" FontSize="12"',
            '                       Opacity="0.5" TextWrapping="Wrap"',
            '                       Text="PCL2 OE 是第三方修改版，与官方 PCL 无关。" />',
        ]
    )
    text = text.replace(anchor, footer + newline + anchor, 1)
    write_source(path, text, path.read_bytes().startswith(b"\xef\xbb\xbf"))
    print(f"  patched {path.relative_to(root)} (third-party footer)")


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

        # Official PCL is fixed-size and cannot be maximized.
        text = text.replace('ResizeMode="CanResize"', 'ResizeMode="CanMinimize"', 1)

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

    settings_labels = {
        "zh-CN.xaml": {
            "Game": "启动",
            "Launcher": "个性化",
            "About": "其他",
            "Tools": "联机",
        },
        "zh-TW.xaml": {
            "Game": "啟動",
            "Launcher": "個人化",
            "About": "其他",
            "Tools": "連線",
        },
        "en-US.xaml": {
            "Game": "Launch",
            "Launcher": "Personalization",
            "About": "Other",
            "Tools": "Multiplayer",
        },
        "en-GB.xaml": {
            "Game": "Launch",
            "Launcher": "Personalisation",
            "About": "Other",
            "Tools": "Multiplayer",
        },
    }
    for filename, mapping in settings_labels.items():
        try:
            path = find_source(root, filename, "Setup.Left.Category.Game")
        except PatchError:
            continue
        text, _ = read_source(path)
        changed = False
        for key, value in mapping.items():
            pattern = (
                rf'(<sys:String x:Key="Setup\.Left\.Category\.{key}">)'
                rf"[^<]*(</sys:String>)"
            )
            text, count = re.subn(pattern, rf"\g<1>{value}\2", text, count=1)
            changed = changed or count == 1
        if changed:
            write_source(path, text, path.read_bytes().startswith(b"\xef\xbb\xbf"))
            print(f"  patched {path.relative_to(root)} (OE settings categories)")


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
        patch_page_load_smoothness(root)
        if args.edition == "oe":
            patch_oe_launch_toggle(root)
            patch_oe_single_profile_flow(root)
            patch_oe_login_panel(root)
            patch_oe_offline_panel(root)
            patch_oe_offline_preview(root)
            patch_oe_shell(root)
            patch_oe_auth_page(root)
            patch_oe_auth_entry(root)
            patch_oe_settings_order(root)
            patch_oe_more_pages(root)
            patch_oe_more_links(root)
            patch_oe_more_flat(root)
            patch_oe_about_actions(root)
            patch_oe_hide_font_card(root)
            patch_oe_more_order(root)
            patch_oe_about_footer(root)
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
