import ffmpeg
from ffprobe import FFProbe as ffprobe
import sys
import os

def combiner(videos, output="video.mp4", SILENCE="./SILENCE.mp3", print_info=True):
    """
    Combines multiple video files into a single output video.
    
    All videos are scaled/padded to the maximum resolution found,
    and any video without audio gets a silent audio track substituted.
    
    Args:
        videos: List of video file paths to combine.
        output: Output file path (default: "video.mp4").
        SILENCE: Path to a silent audio file used for videos without audio.
        print_info: Whether to print progress/info messages.
    """
    maxRes = (0, 0)
    maxFPS = 0
    filteredVids = []

    # Probe each file and collect valid video streams
    for vid in videos:
        r = ffprobe(vid)
        if r.video:
            vm = r.video[0]
            newVid = (vid, vm, r.audio)
            maxRes = (max(int(vm.width), maxRes[0]), max(int(vm.height), maxRes[1]))
            maxFPS = max(vm.framerate, maxFPS)
            filteredVids.append(newVid)
        else:
            if print_info:
                print(f"File {vid} does not contain any video streams, skipping.")

    assert len(filteredVids) > 0, "Error: Found no suitable videos."
    
    preparedVids = []
    for filename, vidProps, audProps in filteredVids:
        # Video stream: scale and pad to maxRes if needed, normalize SAR
        f = ffmpeg.input(filename).video
        if (int(vidProps.width), int(vidProps.height)) != maxRes:
            f = f.filter(
                'scale',
                size=f"{maxRes[0]}x{maxRes[1]}",
                force_original_aspect_ratio="decrease"
            )
            f = f.filter('pad', maxRes[0], maxRes[1], "(ow-iw)/2", "(oh-ih)/2")
        f = f.filter("setsar", "1")
        preparedVids.append(f)

        # Audio stream: use file's audio or substitute silence
        if audProps:
            preparedVids.append(ffmpeg.input(filename).audio)
        else:
            preparedVids.append(ffmpeg.input(SILENCE).audio)
    
    # Concatenate all segments and run
    final = (
        ffmpeg
        .concat(*preparedVids, n=len(filteredVids), v=1, a=1)
        .output(output)
        .global_args("-y")
        .global_args("-vsync", "2")
        .global_args("-hide_banner")
        .global_args("-loglevel", "error")
    )
    final.run()

    if print_info:
        print(f"Finished! Exported video ({maxRes[0]}x{maxRes[1]}p{maxFPS})")


if __name__ == "__main__":
    from os import listdir
    from sys import argv

    output_file = argv[1] if len(argv) > 1 else "video.mp4"
    input_files = (
        argv[2:]
        if len(argv) > 2
        else sorted(["videos/" + i for i in listdir("videos")])
    )
    combiner(input_files, output_file)

    preparedVids = []
    for filename, vidProps, audProps in filteredVids:
        f = ffmpeg.input(filename).video
        if (size := (vidProps.width, vidProps.height)) != maxRes:
            f = f.filter('scale', size = f"{maxRes[0]}x{maxRes[1]}", force_original_aspect_ratio = "decrease")
            f = f.filter('pad', maxRes[0], maxRes[1], "(ow-iw)/2", "(oh-ih)/2")
        f = f.filter("setsar", "1")
        preparedVids.append(f)
        if audProps:
            preparedVids.append(ffmpeg.input(filename).audio)
        else:
            preparedVids.append(ffmpeg.input(SILENCE).audio)
    
    final = ffmpeg.concat(*preparedVids, n = len(filteredVids), v = 1, a = 1).output(output).global_args("-y").global_args("-vsync", "2").global_args("-hide_banner").global_args("-loglevel", "error")
    final.run()
    if print_info: print(f"Finished! Exported video ({maxRes[0]}x{maxRes[1]}p{maxFPS})")

if __name__ == "__main__":
    from os import listdir
    from sys import argv
    combiner(argv[2:] if len(argv) > 2 else sorted(["videos/"+i for i in listdir("videos")]), argv[1] if len(argv) > 1 else "video.mp4")
