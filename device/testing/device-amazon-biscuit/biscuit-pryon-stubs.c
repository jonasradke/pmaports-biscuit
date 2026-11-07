/* Link-time stand-ins for the two libraries biscuit-pryon-helper finds on the device at run time, so that it
 * can be built by anyone, with no Amazon file in the build.  Built twice, as libpryon.so and as libc.so. */
#ifdef STUB_PRYON
#define F(name) void name(void) {}
F(PryonApi_SetLoggingCallback)
F(PryonApi_SetEnumeratedResultCallback)
F(PryonApi_SetAcousticEventDetectionResultCallback)
F(PryonModelSet_New)
F(PryonModelSet_Delete)
F(PryonDecoder_NewPryonMultichannelAudioFormat_Default)
F(PryonDecoder_NewSpotterAudioDecoder)
F(PryonDecoder_Delete)
F(PryonDecoder_PushAudioEventSamples)
F(PryonDecoder_PushClientEvents)
F(WhisperApi_setLogEventHandler)
F(WhisperApi_setEventHandler)
F(WhisperApi_loadWhisperModelset)
F(WhisperApi_createWhisperDetector)
F(WhisperApi_pushAudioEvent)
F(WhisperApi_pushEndOfUtterance)
F(WhisperApi_backlogWait)
F(WhisperApi_pushSessionEnd)
F(WhisperApi_deleteWhisperDetector)
#else
#define F(name) void name(void) {}
F(read)
F(write)
F(snprintf)
F(strstr)
F(strlen)
F(strcmp)
F(atoi)
F(exit)
F(memcpy)
F(memset)
F(__libc_init)
#endif
