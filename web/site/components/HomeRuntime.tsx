"use client";

import { useState } from "react";
import { ArrowRight, Check, Pause, RotateCcw, Wrench, BrainCircuit, Database, ShieldCheck, Server } from "lucide-react";
import { Trident } from "@/components/Trident";
import { HOME } from "@/content/site/home";

/** An interactive architecture illustration, not a hosted agent or fabricated run. */
export function HomeRuntime() {
  const [selected, setSelected] = useState(0);
  const [step, setStep] = useState(0);
  const mode = HOME.modes[selected];
  const advanced = step > 0;
  const recovering = selected === 2;
  const waiting = selected === 1 && !advanced;
  const activeStep = selected === 0 ? step : advanced ? 3 : selected === 1 ? 2 : 1;
  function selectMode(index: number) { setSelected(index); setStep(0); }
  return (
    <div className="runtime-stage">
      <div className="runtime-stage-top"><span><Trident size={21} /> Inside a run</span><span>Interactive overview</span></div>
      <div className="runtime-window">
        <div className="runtime-toolbar"><span className="runtime-window-title"><span /> Psych Runtime</span><div className="runtime-tabs" aria-label="Runtime behavior">{HOME.modes.map((item, index) => <button type="button" aria-pressed={selected === index} key={item.id} onClick={() => selectMode(index)}>{item.label}</button>)}</div></div>
        <div className="runtime-content">
          <div className="runtime-explanation"><span className="runtime-section-number">0{selected + 1} / THE RUNTIME</span><h2>{mode.title}</h2><p>{mode.description}</p><button className="runtime-action" type="button" onClick={() => setStep(selected === 0 ? (step + 1) % 4 : advanced ? 0 : 1)}>{selected !== 0 && advanced ? <>Reset illustration <RotateCcw size={14} /></> : <>{mode.action} <ArrowRight size={14} /></>}</button></div>
          <div className="runtime-illustration" aria-live="polite" aria-atomic="true">
            <div className="runtime-flow" data-waiting={waiting}>
              <div className="runtime-node runtime-model" data-current={selected === 0 && (step === 0 || step === 3)}><span className="runtime-node-icon">{recovering ? <Server size={25} /> : <BrainCircuit size={25} />}</span><strong>{recovering ? "Worker 01" : "Your model"}</strong><small>{recovering ? "Stopped" : "Chooses the action"}</small></div>
              <div className="runtime-connector"><span /></div>
              <div className={`runtime-node runtime-core ${waiting ? "runtime-core-waiting" : ""}`}><span className="runtime-core-mark">{recovering ? <Database size={32} /> : waiting ? <Pause size={32} /> : <Trident size={38} />}</span><strong>{recovering ? "Saved run" : "Psych"}</strong><small>{waiting ? "Waits for you" : recovering ? "Progress recorded" : "Runs the work"}</small></div>
              <div className="runtime-connector"><span /></div>
              <div className="runtime-node runtime-tools" data-current={selected === 0 && step === 2 || selected !== 0 && advanced}><span className="runtime-node-icon">{recovering ? <Server size={25} /> : <Wrench size={25} />}</span><strong>{recovering ? "Worker 02" : "Your tools"}</strong><small>{recovering ? advanced ? "Recovers the run" : "Ready to take over" : waiting ? "Not called yet" : "Do the action"}</small></div>
            </div>
            <div className="runtime-save-line" /><div className="runtime-store"><Database size={13} /> {recovering ? "Durable store" : "Your store"} <span>{recovering ? advanced ? "Lease reclaimed" : "Waiting for lease expiry" : "Steps & results"}</span></div>
            <div className="runtime-event"><span className="runtime-event-icon">{selected === 1 && !advanced ? <Pause size={15} /> : selected === 2 && !advanced ? <Database size={15} /> : <Check size={15} />}</span><div><strong>{selected === 0 ? mode.steps[activeStep] : advanced ? selected === 1 ? "Decision recorded. Run resumes." : "New worker reads the saved history." : mode.status}</strong><span>{selected === 0 ? "Each step becomes part of the run history." : selected === 1 ? advanced ? "The pending tool call can now proceed." : "The selected tool has not executed." : advanced ? "Recovery follows the tool’s retry policy." : "Recovery needs a durable store."}</span></div><ShieldCheck size={16} /></div>
          </div>
        </div>
        <ol className="runtime-progress">{mode.steps.map((label, index) => <li key={label} data-active={index === activeStep}><span>{index < activeStep ? <Check size={11} /> : `0${index + 1}`}</span>{label}</li>)}</ol>
      </div>
    </div>
  );
}
