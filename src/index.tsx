// Copyright (c) 2026 ubermoc. All rights reserved. See LICENSE. WoW Addon Updater, https://github.com/ubermoc/Decky-Loader-Wow-mods-update-
// Provenance: wow-addon-updater/2f44d0b2-b3c2-4726-a93d-480b19a8008e
import { callable, definePlugin } from "@decky/api";
import { ButtonItem, DropdownItem, Field, PanelSection, PanelSectionRow, TextField, ToggleField } from "@decky/ui";
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

const PROVENANCE = "wow-addon-updater/2f44d0b2-b3c2-4726-a93d-480b19a8008e";

const scan = callable<[], ScanResult>("scan");
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
  manual_folder: string | null; // set when the project ID was typed in rather than read from the .toc
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
const checkProjectId = callable<[string, string], { ok: boolean; error: string | null; project_id?: number; latest?: string }>(
  "check_project_id",
);
const setProjectId = callable<[string, string, string], { ok: boolean; error: string | null }>("set_project_id");
const clearProjectId = callable<[string, string], { ok: boolean; error: string | null; removed?: boolean }>(
  "clear_project_id",
);
const updateAddonViaApp = callable<[string, string], { ok: boolean; error: string | null; message?: string }>(
  "update_addon_through_app",
);
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

function AppSection({
  curseforge,
  onDone,
}: {
  curseforge: { app_id: number; launch_options: string };
  onDone: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  return (
    <PanelSection title="CurseForge app">
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
  const [open, setOpen] = useState(false);
  const [viaApp, setViaApp] = useState(false);
  const [selected, setSelected] = useState<string | null>(installs[0]?.addons_path ?? null);
  const [list, setList] = useState<AddonList | null>(null);
  const [checks, setChecks] = useState<Record<string, AddonCheck>>({});
  const [checking, setChecking] = useState(false);
  const [busyId, setBusyId] = useState<number | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  // Project ID editor: which addon folder is being edited, what was typed, what CurseForge said.
  const [editing, setEditing] = useState<string | null>(null);
  const [editMode, setEditMode] = useState<"change" | "remove">("change");
  const [idText, setIdText] = useState("");
  const [idFound, setIdFound] = useState<string | null>(null); // newest file for the typed ID, once checked
  const [idError, setIdError] = useState<string | null>(null);
  const [idBusy, setIdBusy] = useState(false);

  const closeEditor = useCallback(() => {
    setEditing(null);
    setEditMode("change");
    setIdText("");
    setIdFound(null);
    setIdError(null);
  }, []);

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

  // The list is only loaded (and CurseForge only asked) while the Addons row is open.
  useEffect(() => {
    closeEditor();
    if (open && selected) void load(selected);
  }, [open, selected, load, closeEditor]);

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

  const checkId = useCallback(async () => {
    if (!selected) return;
    setIdBusy(true);
    setIdFound(null);
    setIdError(null);
    try {
      const r = await checkProjectId(selected, idText);
      if (r.ok) setIdFound(r.latest ?? "a release");
      else setIdError(r.error ?? "Couldn't check that ID.");
    } catch (err) {
      setIdError(String(err));
    } finally {
      setIdBusy(false);
    }
  }, [selected, idText]);

  const saveId = useCallback(
    async (folder: string) => {
      if (!selected) return;
      setIdBusy(true);
      try {
        const r = await setProjectId(selected, folder, idText);
        if (!r.ok) {
          setIdError(r.error ?? "Couldn't save that ID.");
          return;
        }
        closeEditor();
        setMessage(`Saved. ${folder} now updates like the other addons.`);
        await load(selected);
      } catch (err) {
        setIdError(String(err));
      } finally {
        setIdBusy(false);
      }
    },
    [selected, idText, closeEditor, load],
  );

  const removeId = useCallback(
    async (folder: string) => {
      if (!selected) return;
      setIdBusy(true);
      try {
        const r = await clearProjectId(selected, folder);
        closeEditor();
        setMessage(r.ok ? `Removed the project ID for ${folder}.` : (r.error ?? "Couldn't remove it."));
        await load(selected);
      } catch (err) {
        setIdError(String(err));
      } finally {
        setIdBusy(false);
      }
    },
    [selected, closeEditor, load],
  );

  // The editor shown under an addon. "change": type the number, check it, confirm it.
  // "remove": a one-step confirmation, so a stray press can't delete an ID.
  const renderEditor = (folder: string, mode: "change" | "remove") =>
    mode === "remove" ? (
      <>
        <PanelSectionRow>
          <Field
            focusable
            label={`Remove the project ID for ${folder}?`}
            description="It goes back to having no project ID. Your addon files aren't touched."
            bottomSeparator="none"
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={idBusy} onClick={() => removeId(folder)}>
            Yes, remove it
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={idBusy} onClick={closeEditor}>
            Cancel
          </ButtonItem>
        </PanelSectionRow>
      </>
    ) : (
      <>
        <PanelSectionRow>
          <TextField
            label="CurseForge project ID"
            description="The number under 'Project ID' on the addon's CurseForge page."
            mustBeNumeric
            value={idText}
            disabled={idBusy}
            onChange={(e) => {
              setIdText(e.target.value);
              setIdFound(null);
              setIdError(null);
            }}
          />
        </PanelSectionRow>
        {idError && (
          <PanelSectionRow>
            <Field focusable label={idError} bottomSeparator="none" />
          </PanelSectionRow>
        )}
        {idFound && (
          <PanelSectionRow>
            <Field focusable label={`CurseForge has: ${idFound}`} description="Is this the right addon?" bottomSeparator="none" />
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          {idFound ? (
            <ButtonItem layout="below" disabled={idBusy} onClick={() => saveId(folder)}>
              Yes, save this ID
            </ButtonItem>
          ) : (
            <ButtonItem layout="below" disabled={idBusy || idText.trim() === ""} onClick={checkId}>
              {idBusy ? "Checking..." : "Check ID"}
            </ButtonItem>
          )}
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={idBusy} onClick={closeEditor}>
            Cancel
          </ButtonItem>
        </PanelSectionRow>
      </>
    );

  if (installs.length === 0) return null;

  const total = list?.ok ? list.addons.length + list.untracked.length : null;

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
        <ButtonItem
          layout="below"
          description={open ? "Press to collapse" : "Press to list the addons in this version"}
          onClick={() => setOpen((o) => !o)}
        >
          {`${open ? "\u25BE" : "\u25B8"} Addons${open && total !== null ? ` (${total})` : ""}`}
        </ButtonItem>
      </PanelSectionRow>
      {open && (
        <>
          <PanelSectionRow>
            <ToggleField
              label="Use the CurseForge app (slow)"
              description="Opens CurseForge for each update and presses its own Update button. Uses the WoW version CurseForge has selected."
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
          {list?.ok && total === 0 && (
            <PanelSectionRow>
              <Field focusable label="No addons found in this version" bottomSeparator="none" />
            </PanelSectionRow>
          )}
          {list?.ok &&
            list.addons.map((row) => {
              const check = checks[String(row.project_id)];
              const hasUpdate = check?.status === "update";
              return (
                <div key={row.project_id}>
                  <PanelSectionRow>
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
                  {row.manual_folder &&
                    (editing === row.manual_folder ? (
                      renderEditor(row.manual_folder, editMode)
                    ) : (
                      <PanelSectionRow>
                        <DropdownItem
                          label="Project ID"
                          strDefaultLabel={String(row.project_id)}
                          rgOptions={[
                            { data: "change", label: "Change" },
                            { data: "remove", label: "Remove" },
                          ]}
                          selectedOption={null}
                          disabled={busyId !== null || editing !== null}
                          onChange={(option) => {
                            closeEditor();
                            setEditMode(option.data === "remove" ? "remove" : "change");
                            setEditing(row.manual_folder);
                          }}
                        />
                      </PanelSectionRow>
                    ))}
                </div>
              );
            })}
          {list?.ok &&
            list.untracked.map((name) => (
              <div key={`untracked-${name}`}>
                {editing === name ? (
                  renderEditor(name, "change")
                ) : (
                  <>
                    <PanelSectionRow>
                      <ButtonItem
                        layout="below"
                        disabled={busyId !== null || editing !== null}
                        description="No CurseForge ID in its files, so it can't be updated yet. Press to enter its project ID."
                        onClick={() => {
                          closeEditor();
                          setEditing(name);
                        }}
                      >
                        {`Add a project ID for ${name}`}
                      </ButtonItem>
                    </PanelSectionRow>
                    {viaApp && (
                      <PanelSectionRow>
                        <ButtonItem
                          layout="below"
                          disabled={busyId !== null || editing !== null}
                          description="Uses CurseForge's own record of it, no ID needed."
                          onClick={() => updateThroughApp(name, name)}
                        >
                          {busyId === -1 ? "Working..." : `Update ${name} through CurseForge`}
                        </ButtonItem>
                      </PanelSectionRow>
                    )}
                  </>
                )}
              </div>
            ))}
          {message && (
            <PanelSectionRow>
              <Field focusable label={<div style={{ whiteSpace: "pre-wrap" }}>{message}</div>} bottomSeparator="none" />
            </PanelSectionRow>
          )}
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              disabled={busyId !== null || checking || !selected || editing !== null}
              onClick={() => selected && load(selected)}
            >
              {checking ? "Checking..." : "Check for addon updates"}
            </ButtonItem>
          </PanelSectionRow>
        </>
      )}
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
      <AppSection curseforge={result.curseforge} onDone={refresh} />
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
