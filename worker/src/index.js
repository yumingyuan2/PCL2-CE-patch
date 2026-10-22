/**
 * PCL2-CE-patch upstream release dispatcher.
 *
 * Cloudflare Cron Trigger polls the upstream PCL-CE releases. When the latest
 * stable or beta tag changes, it sends a repository_dispatch event to the
 * patch repository, which triggers the patched build workflow.
 *
 * This exists because GitHub automatically disables scheduled workflows in a
 * public repository after 60 days without repository activity.  A Cloudflare
 * Cron Trigger is not subject to that rule.
 */

const GITHUB_API = "https://api.github.com";
const STATE_KEY = "upstream-release-state";

export default {
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(
      run(env).catch((error) => {
        console.error("PCL2-CE-patch dispatcher failed:", error);
      })
    );
  },

  async fetch(request) {
    const url = new URL(request.url);
    if (url.pathname === "/health") {
      return Response.json({
        ok: true,
        service: "pcl2-ce-patch-dispatcher"
      });
    }
    return new Response("PCL2-CE-patch dispatcher\n", {
      headers: { "content-type": "text/plain; charset=utf-8" }
    });
  }
};

async function run(env) {
  const upstream = required(env, "UPSTREAM_REPOSITORY");
  const repository = required(env, "GITHUB_REPOSITORY");
  const eventTypes = [
    env.DISPATCH_EVENT_TYPE || "upstream-release",
    env.DISPATCH_EVENT_TYPE_OE || "upstream-release-oe"
  ];

  if (!env.GITHUB_TOKEN) {
    throw new Error("GITHUB_TOKEN secret is not set");
  }

  // Public upstream repository: no token is needed, which avoids requiring
  // the patch repository's fine-grained token to have access to other repos.
  const releases = await githubJson(
    `/repos/${upstream}/releases?per_page=30`
  );

  if (!Array.isArray(releases)) {
    throw new Error("Upstream releases response is not an array");
  }

  const published = releases.filter(
    (release) => release && !release.draft && release.published_at
  );
  const latestStable = published.find((release) => !release.prerelease);
  const latestBeta = published.find((release) => release.prerelease);

  const current = {
    stable: latestStable?.tag_name ?? null,
    beta: latestBeta?.tag_name ?? null
  };

  if (!current.stable && !current.beta) {
    console.log("No published upstream releases; nothing to do.");
    return;
  }

  const previous = await readState(env);
  if (
    previous &&
    previous.stable === current.stable &&
    previous.beta === current.beta
  ) {
    console.log("Upstream releases unchanged:", current);
    return;
  }

  console.log("Dispatching patched builds:", { previous, current, eventTypes });
  for (const eventType of eventTypes) {
    await githubJson(
      `/repos/${repository}/dispatches`,
      {
        method: "POST",
        body: JSON.stringify({
          event_type: eventType,
          client_payload: current
        })
      },
      env.GITHUB_TOKEN
    );
  }

  // Only remember the tags after GitHub accepted the dispatch.  If the API
  // call fails, the next cron run retries instead of losing the release.
  await env.STATE.put(STATE_KEY, JSON.stringify(current));
  console.log("Dispatched and state updated.");
}

async function readState(env) {
  try {
    const value = await env.STATE.get(STATE_KEY, "json");
    return value && typeof value === "object" ? value : null;
  } catch (error) {
    console.warn("Could not read state from KV; treating it as empty.", error);
    return null;
  }
}

async function githubJson(path, init = {}, token = null) {
  const headers = new Headers(init.headers || {});
  headers.set("Accept", "application/vnd.github+json");
  headers.set("X-GitHub-Api-Version", "2022-11-28");
  headers.set("User-Agent", "pcl2-ce-patch-dispatcher");
  if (init.body && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  const response = await fetch(`${GITHUB_API}${path}`, {
    ...init,
    headers
  });

  const text = await response.text();
  if (!response.ok) {
    throw new Error(
      `${init.method || "GET"} ${path} failed: ${response.status} ${text.slice(0, 500)}`
    );
  }

  return text ? JSON.parse(text) : null;
}

function required(env, name) {
  const value = env[name];
  if (!value) {
    throw new Error(`${name} is not configured`);
  }
  return value;
}
