// Voice-call plumbing for the Playground: WebRTC to OpenAI Realtime with the
// supervisor steering each turn. The ephemeral secret comes from the backend;
// the flow per turn is: caller speaks → transcript event → /voice-turn →
// session.update(instructions) → response.create → agent speaks.
import { api } from "../api";

export type VoiceEvents = {
  onStatus: (s: string) => void;
  onUserUtterance: (text: string) => void;
  onAgentReply: (text: string) => void;
  onTurnResult: (r: any) => void;
  onEnded: (reason: string) => void;
};

export class VoiceCall {
  private pc: RTCPeerConnection | null = null;
  private dc: RTCDataChannel | null = null;
  private mic: MediaStream | null = null;
  private audioEl: HTMLAudioElement;
  private lastAgentReply: string | null = null;
  private flavor: "ga" | "beta" = "ga";
  private planning = false;

  constructor(
    private sessionId: string,
    private events: VoiceEvents,
  ) {
    this.audioEl = new Audio();
    this.audioEl.autoplay = true;
  }

  async start(): Promise<void> {
    this.events.onStatus("requesting microphone…");
    this.mic = await navigator.mediaDevices.getUserMedia({ audio: true });

    this.events.onStatus("minting call token…");
    const token = await api("POST", `/sessions/${this.sessionId}/realtime-token`);
    this.flavor = token.api_flavor === "beta" ? "beta" : "ga";

    this.events.onStatus("connecting call…");
    const pc = new RTCPeerConnection();
    this.pc = pc;
    pc.ontrack = (e) => {
      this.audioEl.srcObject = e.streams[0];
    };
    for (const track of this.mic.getTracks()) pc.addTrack(track, this.mic);

    const dc = pc.createDataChannel("oai-events");
    this.dc = dc;
    dc.onmessage = (e) => this.handleEvent(JSON.parse(e.data));
    pc.onconnectionstatechange = () => {
      if (pc.connectionState === "failed" || pc.connectionState === "disconnected") {
        this.events.onEnded(`connection ${pc.connectionState}`);
      }
    };

    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    const url = this.flavor === "ga" ? token.webrtc_url_ga : token.webrtc_url_beta;
    const res = await fetch(url, {
      method: "POST",
      headers: { Authorization: `Bearer ${token.client_secret}`, "Content-Type": "application/sdp" },
      body: offer.sdp,
    });
    if (!res.ok) throw new Error(`realtime SDP exchange failed: ${res.status} ${await res.text()}`);
    await pc.setRemoteDescription({ type: "answer", sdp: await res.text() });
    this.events.onStatus("listening — say hello");
  }

  private send(obj: unknown): void {
    this.dc?.send(JSON.stringify(obj));
  }

  private async handleEvent(ev: any): Promise<void> {
    const t = ev.type as string;
    if (t === "conversation.item.input_audio_transcription.completed") {
      const text = (ev.transcript || "").trim();
      if (text && !this.planning) {
        this.events.onUserUtterance(text);
        await this.runTurn(text);
      }
      return;
    }
    if (t === "response.output_audio_transcript.done" || t === "response.audio_transcript.done") {
      const text = (ev.transcript || "").trim();
      if (text) {
        this.lastAgentReply = text;
        this.events.onAgentReply(text);
      }
      return;
    }
    if (t === "response.done") {
      this.events.onStatus("listening…");
      return;
    }
    if (t === "error") {
      this.events.onStatus(`realtime error: ${ev.error?.message ?? "unknown"}`);
    }
  }

  private async runTurn(userText: string): Promise<void> {
    this.planning = true;
    this.events.onStatus("supervisor planning…");
    try {
      const r = await api("POST", `/sessions/${this.sessionId}/voice-turn`, {
        user_message: userText,
        prev_assistant_message: this.lastAgentReply,
      });
      this.events.onTurnResult(r);
      const session =
        this.flavor === "ga"
          ? { type: "realtime", instructions: r.instructions }
          : { instructions: r.instructions };
      this.send({ type: "session.update", session });
      this.send({ type: "response.create" });
      this.events.onStatus("agent speaking…");
      if (r.terminal) {
        // let the goodbye play out, then end
        setTimeout(() => this.events.onEnded(`terminal: ${r.terminal}`), 6000);
      }
    } catch (e) {
      this.events.onStatus(`turn failed: ${e}`);
    } finally {
      this.planning = false;
    }
  }

  stop(): void {
    this.dc?.close();
    this.pc?.close();
    this.mic?.getTracks().forEach((t) => t.stop());
    this.audioEl.srcObject = null;
    this.pc = null;
    this.dc = null;
    this.mic = null;
  }
}
