// screen-runtime.ts - Workflow orchestration runtime for DOM-based screen loading
// This file is deterministic (not LLM generated) to keep runtime stable across screens.
export {};

declare global {
  interface Window {
    __AIQOD_SCREEN__?: {
      runtimeVersion: string;
      screenId: string | null;
      input: any;
      listeners: Array<(data: any) => void>;
      _output: any;
      _resolve?: (output: any) => void;
      setScreenId: (id: string) => void;
      setInput: (data: any) => void;
      onInput: (cb: (data: any) => void) => void;
      complete: (output: any) => void;
      getOutput: () => any;
      waitForCompletion: () => Promise<any>;
      getObsContext: () => {
        execution_id?: string;
        workflow_id?: string;
        screen_id?: string;
        user_id?: string;
        userId?: string;
        subscriber_id?: string;
        subscriberId?: string;
        org_id?: string;
        orgId?: string;
        _obs_ui_span_id?: string;
      };
    };
    __SCREEN_REF__?: any;
  }
}

(function() {
  const runtimeVersion = "1.0.0";
  const w = window as any;

  function buildObsContext(screen: any) {
    const input = (screen && screen.input) || {};
    const userId = input.user_id || input.userId;
    const subscriberId = input.subscriber_id || input.subscriberId;
    const orgId = input.org_id || input.orgId;
    return {
      execution_id: input.execution_id || input.executionId,
      workflow_id: input.workflow_id || input.workflowId,
      screen_id: (screen && screen.screenId) || input.screen_id || input.screenId,
      user_id: userId,
      userId,
      subscriber_id: subscriberId,
      subscriberId,
      org_id: orgId,
      orgId,
      _obs_ui_span_id: input._obs_ui_span_id,
    };
  }

  function mergeObsMeta(requestMeta: any, obs: any) {
    const meta = requestMeta && typeof requestMeta === "object" ? { ...requestMeta } : {};
    if (!meta.source) meta.source = "angular_app";
    if (!meta.execution_id && obs.execution_id) meta.execution_id = obs.execution_id;
    if (!meta.workflow_id && obs.workflow_id) meta.workflow_id = obs.workflow_id;
    if (!meta.screen_id && obs.screen_id) meta.screen_id = obs.screen_id;
    if (!meta.user_id && !meta.userId && (obs.user_id || obs.userId)) {
      meta.user_id = obs.user_id || obs.userId;
      meta.userId = obs.user_id || obs.userId;
    }
    if (!meta.subscriber_id && !meta.subscriberId && (obs.subscriber_id || obs.subscriberId)) {
      meta.subscriber_id = obs.subscriber_id || obs.subscriberId;
      meta.subscriberId = obs.subscriber_id || obs.subscriberId;
    }
    if (!meta.org_id && !meta.orgId && (obs.org_id || obs.orgId)) {
      meta.org_id = obs.org_id || obs.orgId;
      meta.orgId = obs.org_id || obs.orgId;
    }
    if (!meta._obs_ui_span_id && obs._obs_ui_span_id) meta._obs_ui_span_id = obs._obs_ui_span_id;
    return meta;
  }

  function patchDynamicExecuteBody(body: any): any {
    if (typeof body !== "string") return body;
    try {
      const parsed = JSON.parse(body);
      if (!parsed || typeof parsed !== "object" || !parsed.function_id) return body;
      const screen = w.__AIQOD_SCREEN__;
      const obs = screen && typeof screen.getObsContext === "function"
        ? screen.getObsContext()
        : buildObsContext(screen);
      parsed.request_meta = mergeObsMeta(parsed.request_meta, obs);
      return JSON.stringify(parsed);
    } catch (_e) {
      return body;
    }
  }

  function installExecuteInterceptor() {
    if (w.__AIQOD_OBS_HTTP__) return;
    w.__AIQOD_OBS_HTTP__ = true;

    const origFetch = w.fetch;
    if (typeof origFetch === "function") {
      w.fetch = function(input: any, init?: any) {
        try {
          const url = typeof input === "string" ? input : (input && input.url) || "";
          if (/dynamic\/execute/.test(String(url)) && init && init.body) {
            init = { ...init, body: patchDynamicExecuteBody(init.body) };
          }
        } catch (_e) {}
        return origFetch.call(this, input, init);
      };
    }

    const XHR = w.XMLHttpRequest;
    if (XHR && XHR.prototype) {
      const origOpen = XHR.prototype.open;
      const origSend = XHR.prototype.send;
      XHR.prototype.open = function(method: string, url: string) {
        this.__aiqod_url = url;
        return origOpen.apply(this, arguments as any);
      };
      XHR.prototype.send = function(body?: any) {
        try {
          if (this.__aiqod_url && /dynamic\/execute/.test(String(this.__aiqod_url))) {
            body = patchDynamicExecuteBody(body);
          }
        } catch (_e) {}
        return origSend.call(this, body);
      };
    }
  }

  if (!w.__AIQOD_SCREEN__) {
    w.__AIQOD_SCREEN__ = {
      runtimeVersion,
      screenId: null,
      input: {},
      listeners: [],
      _output: null,
      _resolve: null,

      setScreenId(id: string) {
        this.screenId = id;
      },

      setInput(data: any) {
        try {
          this.input = JSON.parse(JSON.stringify(data || {}));
        } catch(e) {
          this.input = data || {};
        }
        this.listeners.forEach((fn: any) => {
          try {
            fn(this.input);
          } catch(err) {
            console.error("Error in input listener:", err);
          }
        });
      },

      onInput(cb: (data: any) => void) {
        this.listeners.push(cb);
        if (Object.keys(this.input || {}).length > 0) {
          try {
            cb(this.input);
          } catch(err) {
            console.error("Error calling input callback:", err);
          }
        }
      },

      complete(output: any) {
        this._output = output;
        if (this._resolve) {
          this._resolve(output);
          this._resolve = null;
        }
      },

      getOutput() {
        return this._output || null;
      },

      waitForCompletion(): Promise<any> {
        return new Promise((res) => {
          this._resolve = res;
        });
      },

      getObsContext() {
        return buildObsContext(this);
      }
    };
  } else if (typeof w.__AIQOD_SCREEN__.getObsContext !== "function") {
    w.__AIQOD_SCREEN__.getObsContext = function() {
      return buildObsContext(this);
    };
  }

  installExecuteInterceptor();
})();
