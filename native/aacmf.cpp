// aacmf.dll: AAC-LC encoding through Windows' own Media Foundation AAC encoder (already
// licensed by Microsoft, so Bluetooth Multicodec ships no AAC code). Plain C API for ctypes:
//
//   void* aacmf_open(int sample_rate, int channels, int bytes_per_second, long* hr_out)
//   int   aacmf_encode(void* h, const short* pcm, int frames,
//                      unsigned char* out, int out_capacity, int* sizes, int max_units)
//   void  aacmf_close(void* h)
//
// pcm is interleaved s16le; aacmf_encode returns the number of raw AAC access units written
// back to back into `out` (each 1024 samples), with their byte sizes in `sizes`, or -1 on error.
// bytes_per_second must be one of 12000/16000/20000/24000 (96/128/160/192 kbps).

#include <windows.h>
#include <mfapi.h>
#include <mferror.h>
#include <mfidl.h>
#include <mftransform.h>
#include <wmcodecdsp.h>

#include <new>

#pragma comment(lib, "mfplat.lib")
#pragma comment(lib, "mfuuid.lib")
#pragma comment(lib, "wmcodecdspuuid.lib")
#pragma comment(lib, "ole32.lib")

struct Encoder {
    IMFTransform* mft = nullptr;
    int channels = 2;
    int sample_rate = 48000;
    DWORD out_size = 0;
    bool provides_samples = false;
    LONGLONG time = 0;
    bool com_initialized = false;  // our CoInitializeEx succeeded, so we owe a CoUninitialize
    bool mf_started = false;       // MFStartup succeeded, so we owe an MFShutdown
};

static HRESULT set_types(Encoder* e, int bytes_per_second) {
    IMFMediaType* out = nullptr;
    IMFMediaType* in = nullptr;
    HRESULT hr = MFCreateMediaType(&out);
    if (SUCCEEDED(hr)) hr = out->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Audio);
    if (SUCCEEDED(hr)) hr = out->SetGUID(MF_MT_SUBTYPE, MFAudioFormat_AAC);
    if (SUCCEEDED(hr)) hr = out->SetUINT32(MF_MT_AUDIO_BITS_PER_SAMPLE, 16);
    if (SUCCEEDED(hr)) hr = out->SetUINT32(MF_MT_AUDIO_SAMPLES_PER_SECOND, e->sample_rate);
    if (SUCCEEDED(hr)) hr = out->SetUINT32(MF_MT_AUDIO_NUM_CHANNELS, e->channels);
    if (SUCCEEDED(hr)) hr = out->SetUINT32(MF_MT_AUDIO_AVG_BYTES_PER_SECOND, bytes_per_second);
    if (SUCCEEDED(hr)) hr = out->SetUINT32(MF_MT_AAC_PAYLOAD_TYPE, 0);  // raw access units
    if (SUCCEEDED(hr)) hr = out->SetUINT32(MF_MT_AAC_AUDIO_PROFILE_LEVEL_INDICATION, 0x29);  // AAC-LC
    // Encoders take the output type first
    if (SUCCEEDED(hr)) hr = e->mft->SetOutputType(0, out, 0);

    if (SUCCEEDED(hr)) hr = MFCreateMediaType(&in);
    if (SUCCEEDED(hr)) hr = in->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Audio);
    if (SUCCEEDED(hr)) hr = in->SetGUID(MF_MT_SUBTYPE, MFAudioFormat_PCM);
    if (SUCCEEDED(hr)) hr = in->SetUINT32(MF_MT_AUDIO_BITS_PER_SAMPLE, 16);
    if (SUCCEEDED(hr)) hr = in->SetUINT32(MF_MT_AUDIO_SAMPLES_PER_SECOND, e->sample_rate);
    if (SUCCEEDED(hr)) hr = in->SetUINT32(MF_MT_AUDIO_NUM_CHANNELS, e->channels);
    if (SUCCEEDED(hr)) hr = in->SetUINT32(MF_MT_AUDIO_BLOCK_ALIGNMENT, 2 * e->channels);
    if (SUCCEEDED(hr)) hr = in->SetUINT32(MF_MT_AUDIO_AVG_BYTES_PER_SECOND, 2 * e->channels * e->sample_rate);
    if (SUCCEEDED(hr)) hr = e->mft->SetInputType(0, in, 0);

    if (out) out->Release();
    if (in) in->Release();
    return hr;
}

extern "C" __declspec(dllexport) void aacmf_close(void* handle) {
    auto* e = static_cast<Encoder*>(handle);
    if (!e) return;
    if (e->mft) e->mft->Release();
    if (e->mf_started) MFShutdown();
    if (e->com_initialized) CoUninitialize();
    delete e;
}

extern "C" __declspec(dllexport) void* aacmf_open(int sample_rate, int channels, int bytes_per_second, long* hr_out) {
    auto* e = new (std::nothrow) Encoder();
    if (!e) {
        if (hr_out) *hr_out = E_OUTOFMEMORY;
        return nullptr;
    }
    HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    e->com_initialized = SUCCEEDED(hr);  // S_OK or S_FALSE both need balancing
    if (hr == RPC_E_CHANGED_MODE) hr = S_OK;  // the thread already has COM in another mode; usable as is
    if (SUCCEEDED(hr)) {
        hr = MFStartup(MF_VERSION, MFSTARTUP_LITE);
        e->mf_started = SUCCEEDED(hr);
    }
    if (SUCCEEDED(hr)) {
        e->channels = channels;
        e->sample_rate = sample_rate;
        // Missing on Windows "N" editions without the Media Feature Pack
        hr = CoCreateInstance(CLSID_AACMFTEncoder, nullptr, CLSCTX_INPROC_SERVER, IID_PPV_ARGS(&e->mft));
    }
    if (SUCCEEDED(hr)) hr = set_types(e, bytes_per_second);
    MFT_OUTPUT_STREAM_INFO info = {};
    if (SUCCEEDED(hr)) hr = e->mft->GetOutputStreamInfo(0, &info);
    if (SUCCEEDED(hr)) {
        e->out_size = info.cbSize ? info.cbSize : 8192;
        e->provides_samples = (info.dwFlags & MFT_OUTPUT_STREAM_PROVIDES_SAMPLES) != 0;
        hr = e->mft->ProcessMessage(MFT_MESSAGE_NOTIFY_BEGIN_STREAMING, 0);
    }
    if (hr_out) *hr_out = hr;
    if (FAILED(hr)) {
        aacmf_close(e);
        return nullptr;
    }
    return e;
}

static HRESULT drain(Encoder* e, unsigned char* out, int out_capacity, int* sizes, int max_units, int* units, int* used) {
    for (;;) {
        MFT_OUTPUT_DATA_BUFFER output = {};
        IMFSample* sample = nullptr;
        IMFMediaBuffer* buffer = nullptr;
        if (!e->provides_samples) {
            HRESULT hr = MFCreateSample(&sample);
            if (SUCCEEDED(hr)) hr = MFCreateMemoryBuffer(e->out_size, &buffer);
            if (SUCCEEDED(hr)) hr = sample->AddBuffer(buffer);
            if (FAILED(hr)) {
                if (buffer) buffer->Release();
                if (sample) sample->Release();
                return hr;
            }
            output.pSample = sample;
        }
        DWORD status = 0;
        HRESULT hr = e->mft->ProcessOutput(0, 1, &output, &status);
        if (output.pEvents) output.pEvents->Release();
        if (hr == MF_E_TRANSFORM_NEED_MORE_INPUT) {
            if (buffer) buffer->Release();
            if (sample) sample->Release();
            return S_OK;
        }
        if (SUCCEEDED(hr)) {
            IMFMediaBuffer* contiguous = nullptr;
            hr = output.pSample->ConvertToContiguousBuffer(&contiguous);
            BYTE* data = nullptr;
            DWORD length = 0;
            if (SUCCEEDED(hr)) hr = contiguous->Lock(&data, nullptr, &length);
            if (SUCCEEDED(hr)) {
                if (*units < max_units && *used + (int)length <= out_capacity) {
                    memcpy(out + *used, data, length);
                    sizes[(*units)++] = (int)length;
                    *used += (int)length;
                } else {
                    hr = E_NOT_SUFFICIENT_BUFFER;
                }
                contiguous->Unlock();
            }
            if (contiguous) contiguous->Release();
        }
        if (e->provides_samples && output.pSample) output.pSample->Release();
        if (buffer) buffer->Release();
        if (sample) sample->Release();
        if (FAILED(hr)) return hr;
    }
}

extern "C" __declspec(dllexport) int aacmf_encode(void* handle, const short* pcm, int frames,
                                                  unsigned char* out, int out_capacity, int* sizes, int max_units) {
    auto* e = static_cast<Encoder*>(handle);
    DWORD bytes = (DWORD)frames * 2 * e->channels;
    IMFSample* sample = nullptr;
    IMFMediaBuffer* buffer = nullptr;
    BYTE* data = nullptr;
    HRESULT hr = MFCreateMemoryBuffer(bytes, &buffer);
    if (SUCCEEDED(hr)) hr = buffer->Lock(&data, nullptr, nullptr);
    if (SUCCEEDED(hr)) {
        memcpy(data, pcm, bytes);
        buffer->Unlock();
        hr = buffer->SetCurrentLength(bytes);
    }
    if (SUCCEEDED(hr)) hr = MFCreateSample(&sample);
    if (SUCCEEDED(hr)) hr = sample->AddBuffer(buffer);
    LONGLONG duration = (LONGLONG)frames * 10000000LL / e->sample_rate;
    if (SUCCEEDED(hr)) hr = sample->SetSampleTime(e->time);
    if (SUCCEEDED(hr)) hr = sample->SetSampleDuration(duration);
    e->time += duration;
    if (SUCCEEDED(hr)) hr = e->mft->ProcessInput(0, sample, 0);
    int units = 0, used = 0;
    if (SUCCEEDED(hr)) hr = drain(e, out, out_capacity, sizes, max_units, &units, &used);
    if (buffer) buffer->Release();
    if (sample) sample->Release();
    return FAILED(hr) ? -1 : units;
}
