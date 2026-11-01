// Copyright (c) 2026 ubermoc. All rights reserved. See LICENSE. WoW Addon Updater, https://github.com/ubermoc/Decky-Loader-Wow-mods-update-
// Provenance: wow-addon-updater/2f44d0b2-b3c2-4726-a93d-480b19a8008e
import { callable, definePlugin } from "@decky/api";
import { ButtonItem, DropdownItem, Field, PanelSection, PanelSectionRow, ToggleField } from "@decky/ui";
import { useCallback, useEffect, useState } from "react";
import { FaCloudDownloadAlt } from "react-icons/fa";

interface WowInstall {
  flavor: string;
  label: string;
  root: string;
  addons_path: string;
  addon_count: number;
  addons_modified: number | null;
}

interface ScanResult {
  ok: boolean;
  error: string | null;
  curseforge: { app_id: number; name: string; exe: string; launch_options: string } | null;
  installs: WowInstall[];
}

interface UpdateInfo {
  installed: { version: string; build: string };
  latest: { version: string | null; build: string | null } | null;
  update_available: boolean;
  error: string | null;
}

interface InstallResult {
  ok: boolean;
  error: string | null;
  installed: { version: string | null; build: string | null } | null;
}

interface AddonRunResult {
  ok: boolean;
  error: string | null;
  finished?: number;
  results: {
    label: string;
    addons_path: string;
    tracked: number;
    untracked: number;
    untracked_names?: string[];
    current: number;
    incompatible: number;
    updated: string[];
    failed: { addon: string; error: string }[];
    error: string | null;
  }[];
}

interface AddonSettings {
  auto_update: boolean;
  last_run: AddonRunResult | null;
}

// Kept in the bundle on purpose: identifies this plugin's origin.
const PROVENANCE = "wow-addon-updater/2f44d0b2-b3c2-4726-a93d-480b19a8008e";

const scan = callable<[], ScanResult>("scan");
const updateAddons = callable<[string], AddonRunResult>("update_addons");
const getAddonSettings = callable<[], AddonSettings>("get_addon_settings");
const prepareApp = callable<[], { ok: boolean; error: string | null }>("prepare_curseforge_app");
const updateViaApp = callable<[], { ok: boolean; error: string | null; clicked?: boolean; message?: string }>(
  "update_via_curseforge_app",
);
interface AddonRow {
  project_id: number;
  name: string;
  folders: string[];
  version: string;
  installed_file_known: boolean;
}

interface AddonList {
  ok: boolean;
  error: string | null;
  label?: string;
  addons: AddonRow[];
  untracked: string[];
}

interface AddonCheck {
  status: "update" | "current" | "incompatible" | "error";
  latest: string | null;
  error: string | null;
}

const listAddons = callable<[string], AddonList>("list_addons");
const checkAddons = callable<[string], { ok: boolean; error: string | null; results: Record<string, AddonCheck> }>(
  "check_addons",
);
const updateAddon = callable<[string, number], { ok: boolean; error: string | null; latest?: string }>("update_addon");
const diagnoseApp = callable<[], { ok: boolean; error: string | null; summary?: string; path?: string }>(
  "diagnose_curseforge_app",
);
const updateAddonViaApp = callable<[string, string], { ok: boolean; error: string | null; message?: string }>(
  "update_addon_through_app",
);
const setAutoUpdate = callable<[boolean], { auto_update: boolean }>("set_auto_update");
const getUpdateInfo = callable<[], UpdateInfo>("get_update_info");
const installUpdate = callable<[], InstallResult>("install_update");

function describeBuild(version: string | null | undefined, build: string | null | undefined): string {
  if (!build) return version ?? "unknown";
  const short = build.slice(0, 7);
  return version && version !== "unknown" ? `${version} (${short})` : short;
}

// Steam's game id for a non-Steam shortcut is (appid << 32) | 0x02000000.
// The appid from shortcuts.vdf is an unsigned 32-bit value, so BigInt keeps it exact.
function shortcutGameId(appId: number): string {
  return ((BigInt(appId >>> 0) << 32n) | 0x02000000n).toString();
}

const DEBUG_FLAG = "--remote-debugging-port=9222";

// The shortcut's own launch options plus the debugging flag, for ONE launch only (nothing in Steam
// is edited). Existing options are kept in case Steam replaces rather than appends them.
function withDebugFlag(existing: string): string {
  return existing.includes("--remote-debugging-port") ? existing : `${existing} ${DEBUG_FLAG}`.trim();
}

function launchShortcut(appId: number, launchOptions = ""): void {
  // Typed loosely on purpose: this API is undocumented and unverified on a real Deck.
  const steam = (window as unknown as { SteamClient?: { Apps?: { RunGame?: (...a: unknown[]) => void } } })
    .SteamClient;
  if (!steam?.Apps?.RunGame) throw new Error("SteamClient.Apps.RunGame is not available");
  steam.Apps.RunGame(shortcutGameId(appId), launchOptions, -1, 100);
}

// addons_modified per AddOns folder at the moment CurseForge was launched. Module-level so it
// survives the quick-access panel being closed and reopened while CurseForge runs.
let launchSnapshot: Record<string, number | null> | null = null;

function relativeAge(epochSeconds: number): string {
  const mins = Math.max(0, Math.round((Date.now() / 1000 - epochSeconds) / 60));
  if (mins < 2) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  return hours < 24 ? `${hours} h ago` : formatModified(epochSeconds);
}

function formatModified(epochSeconds: number | null): string {
  if (epochSeconds === null) return "never";
  return new Date(epochSeconds * 1000).toLocaleString();
}

function SetupInstructions({ onRecheck }: { onRecheck: () => void }) {
  return (
    <PanelSection title="Set up CurseForge">
      <PanelSectionRow>
        <Field focusable label="CurseForge isn't in your Steam library yet" bottomSeparator="none">
          <div style={{ whiteSpace: "normal", lineHeight: 1.4 }}>
            1. Switch to Desktop Mode.
            <br />
            2. Download the CurseForge app for Linux (AppImage) from curseforge.com/download/app and mark it
            executable.
            <br />
            3. In Steam choose Add a Game, then Add a Non-Steam Game, and select the AppImage.
            <br />
            4. Return to Gaming Mode and press Check again.
          </div>
        </Field>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" onClick={onRecheck}>
          Check again
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
}

function InstallList({ installs, updated }: { installs: WowInstall[]; updated: Set<string> }) {
  if (installs.length === 0) {
    return (
      <PanelSection title="World of Warcraft">
        <PanelSectionRow>
          <Field focusable label="No WoW installs found" bottomSeparator="none">
            <div style={{ whiteSpace: "normal" }}>
              Launch WoW once from Battle.net in Gaming Mode so its Proton prefix exists, then check again.
            </div>
          </Field>
        </PanelSectionRow>
      </PanelSection>
    );
  }
  return (
    <PanelSection title="World of Warcraft">
      {installs.map((install) => (
        <PanelSectionRow key={install.addons_path}>
          <Field
            focusable
            label={install.label}
            description={
              updated.has(install.addons_path) && install.addons_modified !== null
                ? `${install.addon_count} addons - Updated ${relativeAge(install.addons_modified)}`
                : `${install.addon_count} addons - AddOns changed ${formatModified(install.addons_modified)}`
            }
            bottomSeparator="none"
          />
        </PanelSectionRow>
      ))}
    </PanelSection>
  );
}

function summarizeRun(run: AddonRunResult): string {
  if (!run.ok) return run.error ?? "Addon update failed.";
  if (run.results.length === 0) return "No WoW versions to update.";
  return run.results
    .map((r) => {
      if (r.error) return `${r.label}: ${r.error}`;
      const parts: string[] = [];
      if (r.updated.length) parts.push(`${r.updated.length} updated (${r.updated.slice(0, 4).join(", ")}${r.updated.length > 4 ? ", ..." : ""})`);
      if (r.current) parts.push(`${r.current} already current`);
      if (r.incompatible) parts.push(`${r.incompatible} have no release for this version`);
      if (r.failed.length) parts.push(`${r.failed.length} failed (${r.failed[0].addon}: ${r.failed[0].error})`);
      if (r.untracked) parts.push(`${r.untracked} have no CurseForge ID, can't be auto-updated (${(r.untracked_names ?? []).join(", ")}${r.untracked > (r.untracked_names ?? []).length ? ", ..." : ""})`);
      if (r.tracked === 0) parts.unshift("no CurseForge addons found");
      return `${r.label}: ${parts.join("; ")}`;
    })
    .join("\n");
}

function AddonUpdates({
  installs,
  curseforge,
  onDone,
}: {
  installs: WowInstall[];
  curseforge: { app_id: number; launch_options: string };
  onDone: () => void;
}) {
  const [settings, setSettings] = useState<AddonSettings | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        const s = await getAddonSettings();
        setSettings(s);
        if (s.last_run?.finished) {
          setMessage(`Last automatic run ${relativeAge(s.last_run.finished)}:\n${summarizeRun(s.last_run)}`);
        }
      } catch (err) {
        setMessage(`Could not load addon settings: ${String(err)}`);
      }
    })();
  }, []);

  const run = useCallback(
    async (path: string) => {
      setBusy(true);
      setMessage("Updating addons... this can take a few minutes.");
      try {
        const result = await updateAddons(path);
        setMessage(summarizeRun(result));
        onDone();
      } catch (err) {
        setMessage(`Addon update failed: ${String(err)}`);
      } finally {
        setBusy(false);
      }
    },
    [onDone],
  );

  return (
    <PanelSection title="Addon updates">
      <PanelSectionRow>
        <ToggleField
          label="Update automatically"
          description="Shortly after Decky starts, then every 6 hours. Waits while WoW is running."
          checked={settings?.auto_update ?? true}
          disabled={settings === null}
          onChange={async (checked: boolean) => {
            setSettings((s) => (s ? { ...s, auto_update: checked } : s));
            try {
              await setAutoUpdate(checked);
            } catch (err) {
              setMessage(`Could not save the setting: ${String(err)}`);
            }
          }}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={busy || installs.length === 0} onClick={() => run("")}>
          {busy ? "Working..." : "Update all addons now"}
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          disabled={busy}
          description="Opens CurseForge, presses its Update all button, then closes it. Open WoW > My Addons in CurseForge once so it reopens there."
          onClick={async () => {
            setBusy(true);
            setMessage("Preparing CurseForge...");
            try {
              const prep = await prepareApp();
              if (!prep.ok) {
                setMessage(prep.error ?? "Couldn't prepare CurseForge.");
                return;
              }
              setMessage("Opening CurseForge...");
              launchShortcut(curseforge.app_id, withDebugFlag(curseforge.launch_options));
              setMessage("Waiting for CurseForge to start and update. This can take a few minutes.");
              const result = await updateViaApp();
              setMessage(result.ok ? (result.message ?? "Done.") : (result.error ?? "Failed."));
              onDone();
            } catch (err) {
              setMessage(`Could not drive CurseForge: ${String(err)}`);
            } finally {
              setBusy(false);
            }
          }}
        >
          Update through the CurseForge app
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          disabled={busy}
          description="Opens CurseForge, reads how its My Addons screen is built, saves it to the plugin's settings folder and shows a summary. For fixing the slow update route."
          onClick={async () => {
            setBusy(true);
            setMessage("Preparing CurseForge...");
            try {
              const prep = await prepareApp();
              if (!prep.ok) {
                setMessage(prep.error ?? "Couldn't prepare CurseForge.");
                return;
              }
              setMessage("Opening CurseForge...");
              launchShortcut(curseforge.app_id, withDebugFlag(curseforge.launch_options));
              const r = await diagnoseApp();
              setMessage(r.ok ? `${r.summary}\nSaved to ${r.path}` : (r.error ?? "Failed."));
            } catch (err) {
              setMessage(`Diagnostic failed: ${String(err)}`);
            } finally {
              setBusy(false);
            }
          }}
        >
          Diagnose CurseForge app
        </ButtonItem>
      </PanelSectionRow>
      {installs.length > 1 &&
        installs.map((install) => (
          <PanelSectionRow key={install.addons_path}>
            <ButtonItem layout="below" disabled={busy} onClick={() => run(install.addons_path)}>
              Update {install.label} only
            </ButtonItem>
          </PanelSectionRow>
        ))}
      {message && (
        <PanelSectionRow>
          <Field focusable label={<div style={{ whiteSpace: "pre-wrap" }}>{message}</div>} bottomSeparator="none" />
        </PanelSectionRow>
      )}
    </PanelSection>
  );
}

function describeCheck(row: AddonRow, check: AddonCheck | undefined, checking: boolean): string {
  const installed = row.version ? `Installed ${row.version}` : "Installed";
  if (checking && !check) return `${installed} - checking CurseForge...`;
  if (!check) return installed;
  switch (check.status) {
    case "update":
      return `${installed} - update available${check.latest ? `: ${check.latest}` : ""}`;
    case "current":
      return `${installed} - up to date${check.latest ? ` (${check.latest})` : ""}`;
    case "incompatible":
      return `${installed} - CurseForge has no release for this WoW version`;
    default:
      return `${installed} - couldn't check: ${check.error ?? "unknown error"}`;
  }
}

function YourAddons({
  installs,
  curseforge,
  onDone,
}: {
  installs: WowInstall[];
  curseforge: { app_id: number; launch_options: string };
  onDone: () => void;
}) {
  const [viaApp, setViaApp] = useState(false);
  const [selected, setSelected] = useState<string | null>(installs[0]?.addons_path ?? null);
  const [list, setList] = useState<AddonList | null>(null);
  const [checks, setChecks] = useState<Record<string, AddonCheck>>({});
  const [checking, setChecking] = useState(false);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  // Keep the selection valid if the list of WoW versions changes after a rescan.
  useEffect(() => {
    if (!installs.some((i) => i.addons_path === selected)) setSelected(installs[0]?.addons_path ?? null);
  }, [installs, selected]);

  const load = useCallback(async (path: string) => {
    setList(null);
    setChecks({});
    setMessage(null);
    try {
      const l = await listAddons(path);
      setList(l);
      if (!l.ok) return;
      setChecking(true);
      try {
        const c = await checkAddons(path);
        if (c.ok) setChecks(c.results);
        else setMessage(c.error ?? "Couldn't check for updates.");
      } finally {
        setChecking(false);
      }
    } catch (err) {
      setMessage(`Couldn't load addons: ${String(err)}`);
    }
  }, []);

  useEffect(() => {
    if (selected) void load(selected);
  }, [selected, load]);

  const updateOne = useCallback(
    async (row: AddonRow) => {
      if (!selected) return;
      setBusyId(row.project_id);
      setMessage(`Updating ${row.name}...`);
      try {
        const r = await updateAddon(selected, row.project_id);
        if (r.ok) {
          setMessage(`${row.name} updated${r.latest ? ` to ${r.latest}` : ""}.`);
          onDone();
          await load(selected);
          return;
        }
        setMessage(`${row.name}: ${r.error ?? "update failed"}`);
      } catch (err) {
        setMessage(`${row.name}: ${String(err)}`);
      } finally {
        setBusyId(null);
      }
    },
    [selected, load, onDone],
  );

  // Slow route: open CurseForge, press that addon's own Update button there, close it again.
  const updateThroughApp = useCallback(
    async (folder: string, label: string) => {
      if (!selected) return;
      setBusyId(-1);
      setMessage(`Preparing CurseForge for ${label}...`);
      try {
        const prep = await prepareApp();
        if (!prep.ok) {
          setMessage(prep.error ?? "Couldn't prepare CurseForge.");
          return;
        }
        setMessage(`Opening CurseForge to update ${label}. This takes about a minute.`);
        launchShortcut(curseforge.app_id, withDebugFlag(curseforge.launch_options));
        const r = await updateAddonViaApp(selected, folder);
        setMessage(r.ok ? (r.message ?? "Done.") : `${label}: ${r.error ?? "failed"}`);
        onDone();
        await load(selected);
      } catch (err) {
        setMessage(`${label}: ${String(err)}`);
      } finally {
        setBusyId(null);
      }
    },
    [selected, curseforge, load, onDone],
  );

  if (installs.length === 0) return null;

  return (
    <PanelSection title="Your addons">
      <PanelSectionRow>
        <DropdownItem
          label="WoW version"
          rgOptions={installs.map((i) => ({ data: i.addons_path, label: i.label }))}
          selectedOption={selected}
          onChange={(option) => setSelected(option.data as string)}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <ToggleField
          label="Use the CurseForge app (slow)"
          description="Opens CurseForge for each update and presses its own Update button. Also works for addons without a CurseForge ID. Uses the WoW version CurseForge has selected."
          checked={viaApp}
          onChange={setViaApp}
        />
      </PanelSectionRow>
      {list === null && !message && (
        <PanelSectionRow>
          <Field label="Loading addons..." bottomSeparator="none" />
        </PanelSectionRow>
      )}
      {list && !list.ok && (
        <PanelSectionRow>
          <Field focusable label={list.error ?? "Couldn't list addons."} bottomSeparator="none" />
        </PanelSectionRow>
      )}
      {list?.ok && list.addons.length === 0 && (
        <PanelSectionRow>
          <Field focusable label="No CurseForge addons found in this version" bottomSeparator="none" />
        </PanelSectionRow>
      )}
      {list?.ok &&
        list.addons.map((row) => {
          const check = checks[String(row.project_id)];
          const hasUpdate = check?.status === "update";
          return (
            <PanelSectionRow key={row.project_id}>
              <ButtonItem
                layout="below"
                disabled={busyId !== null || (!viaApp && check?.status === "incompatible")}
                description={describeCheck(row, check, checking)}
                onClick={() => (viaApp ? updateThroughApp(row.folders[0], row.name) : updateOne(row))}
              >
                {busyId === row.project_id || (busyId === -1 && viaApp)
                  ? "Working..."
                  : viaApp
                    ? `Update ${row.name} through CurseForge`
                    : `${hasUpdate ? "Update" : "Reinstall"} ${row.name}`}
              </ButtonItem>
            </PanelSectionRow>
          );
        })}
      {list?.ok &&
        list.untracked.map((name) => (
          <PanelSectionRow key={`untracked-${name}`}>
            {viaApp ? (
              <ButtonItem
                layout="below"
                disabled={busyId !== null}
                description="No CurseForge ID in its .toc; updated using CurseForge's own record of it."
                onClick={() => updateThroughApp(name, name)}
              >
                {busyId === -1 ? "Working..." : `Update ${name} through CurseForge`}
              </ButtonItem>
            ) : (
              <Field
                focusable
                label={name}
                description="No CurseForge ID in its .toc, so it can't be updated directly. Add a ## X-Curse-Project-ID line, or turn on 'Use the CurseForge app'."
                bottomSeparator="none"
              />
            )}
          </PanelSectionRow>
        ))}
      {message && (
        <PanelSectionRow>
          <Field focusable label={<div style={{ whiteSpace: "pre-wrap" }}>{message}</div>} bottomSeparator="none" />
        </PanelSectionRow>
      )}
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={busyId !== null || checking || !selected} onClick={() => selected && load(selected)}>
          {checking ? "Checking..." : "Check for addon updates"}
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
}

function UpdatesSection() {
  const [info, setInfo] = useState<UpdateInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [installedNow, setInstalledNow] = useState(false);

  const check = useCallback(async () => {
    setBusy(true);
    try {
      setInfo(await getUpdateInfo());
    } catch (err) {
      setMessage(`Update check failed: ${String(err)}`);
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void check();
  }, [check]);

  const install = useCallback(async () => {
    setBusy(true);
    setMessage(null);
    try {
      const result = await installUpdate();
      if (result.ok) {
        setInstalledNow(true);
        setMessage("Updated. Decky is restarting - reopen this menu in a few seconds.");
      } else {
        setMessage(result.error ?? "Update failed.");
      }
    } catch (err) {
      setMessage(`Update failed: ${String(err)}`);
    } finally {
      setBusy(false);
    }
  }, []);

  const installed = info ? describeBuild(info.installed.version, info.installed.build) : "...";
  const latest = info?.latest ? describeBuild(info.latest.version, info.latest.build) : info ? "unavailable" : "...";
  const canInstall = !!info?.update_available && !busy && !installedNow;
  const status = installedNow
    ? "Installed - reload the plugin"
    : info?.error
      ? info.error
      : info?.update_available
        ? "Update available"
        : info
          ? "Up to date"
          : "Checking...";

  return (
    <PanelSection title="Updates">
      <PanelSectionRow>
        <Field focusable label="Installed version" bottomSeparator="none">
          {installed}
        </Field>
      </PanelSectionRow>
      <PanelSectionRow>
        <Field focusable label="Latest version" description={status} bottomSeparator="none">
          {latest}
        </Field>
      </PanelSectionRow>
      {message && (
        <PanelSectionRow>
          <Field focusable label={message} bottomSeparator="none" />
        </PanelSectionRow>
      )}
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={!canInstall} onClick={install}>
          {busy ? "Working..." : "Install update"}
        </ButtonItem>
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem layout="below" disabled={busy} onClick={check}>
          Check for updates
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
}

function MainContent() {
  const [result, setResult] = useState<ScanResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [launchError, setLaunchError] = useState<string | null>(null);

  const refresh =useCallback(async () => {
    setBusy(true);
    try {
      setResult(await scan());
    } catch (err) {
      setResult({ ok: false, error: String(err), curseforge: null, installs: [] });
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Rescan when CurseForge closes. Typed loosely: undocumented Steam API, unverified on a Deck.
  // If the notification never fires, reopening the panel rescans anyway.
  const curseforgeId = result?.curseforge?.app_id;
  useEffect(() => {
    if (curseforgeId === undefined) return;
    const sessions = (
      window as unknown as {
        SteamClient?: {
          GameSessions?: {
            RegisterForAppLifetimeNotifications?: (
              cb: (n: { unAppID: number; bRunning: boolean }) => void,
            ) => { unregister?: () => void } | undefined;
          };
        };
      }
    ).SteamClient?.GameSessions;
    const reg = sessions?.RegisterForAppLifetimeNotifications?.((n) => {
      if (!n.bRunning && n.unAppID >>> 0 === curseforgeId >>> 0) void refresh();
    });
    return () => reg?.unregister?.();
  }, [curseforgeId, refresh]);

  const updated = new Set<string>();
  if (result?.ok && launchSnapshot) {
    for (const install of result.installs) {
      const before = launchSnapshot[install.addons_path];
      if (before !== undefined && install.addons_modified !== null && (before === null || install.addons_modified > before)) {
        updated.add(install.addons_path);
      }
    }
  }

  if (result === null) {
    return (
      <PanelSection>
        <PanelSectionRow>
          <Field label="Scanning..." />
        </PanelSectionRow>
      </PanelSection>
    );
  }

  if (!result.ok) {
    return (
      <PanelSection title="Scan failed">
        <PanelSectionRow>
          <Field focusable label={result.error ?? "Unknown error"} />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={busy} onClick={refresh}>
            Try again
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    );
  }

  // Once the CurseForge shortcut exists the setup message never shows again.
  if (result.curseforge === null) {
    return <SetupInstructions onRecheck={refresh} />;
  }

  return (
    <>
      <InstallList installs={result.installs} updated={updated} />
      <YourAddons installs={result.installs} curseforge={result.curseforge} onDone={refresh} />
      <AddonUpdates installs={result.installs} curseforge={result.curseforge} onDone={refresh} />
      <PanelSection>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={busy}
            onClick={() => {
              try {
                launchShortcut(result.curseforge!.app_id);
                launchSnapshot = Object.fromEntries(result.installs.map((i) => [i.addons_path, i.addons_modified]));
                setLaunchError(null);
              } catch (err) {
                setLaunchError(String(err));
              }
            }}
          >
            Launch CurseForge
          </ButtonItem>
        </PanelSectionRow>
        {launchError && (
          <PanelSectionRow>
            <Field focusable label={`Could not launch: ${launchError}`} bottomSeparator="none" />
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={busy} onClick={refresh}>
            Rescan
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
}

function Content() {
  return (
    <>
      <MainContent />
      <UpdatesSection />
    </>
  );
}

export default definePlugin(() => {
  console.log(`[WoW Addon Updater] ${PROVENANCE}`);
  return {
  name: "WoW Addon Updater",
  titleView: <div>WoW Addon Updater</div>,
  content: <Content />,
  icon: <FaCloudDownloadAlt />,
  onDismount() {},
  };
});
