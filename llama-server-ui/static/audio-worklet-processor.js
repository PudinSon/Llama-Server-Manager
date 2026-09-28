class CallAudioProcessor extends AudioWorkletProcessor {
	process(inputs, outputs) {
		const input = inputs[0]?.[0];
		const output = outputs[0]?.[0];
		if (input) {
			this.port.postMessage(input.slice());
		}
		if (output) {
			output.fill(0);
		}
		return true;
	}
}

registerProcessor('call-audio-processor', CallAudioProcessor);