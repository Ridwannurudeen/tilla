// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Script} from "forge-std/Script.sol";
import {StoreRegistry} from "../src/StoreRegistry.sol";

/// @notice USER-RUN deploy script (build agents never broadcast). Reads DEPLOYER_KEY
///         from the environment and, optionally, a distinct REGISTRAR owner address
///         (defaults to the deployer). Run:
///
///           DEPLOYER_KEY=0x... forge script script/Deploy.s.sol \
///             --rpc-url https://rpc.xlayer.tech --broadcast
///
/// @dev    See README.md for the OKLink contract-verify step. Deploying = on-chain
///         contract creation = gas + key = explicit user approval.
contract Deploy is Script {
    function run() external returns (StoreRegistry registry) {
        uint256 pk = vm.envUint("DEPLOYER_KEY");
        address registrar = vm.envOr("REGISTRAR", vm.addr(pk));
        vm.startBroadcast(pk);
        registry = new StoreRegistry(registrar);
        vm.stopBroadcast();
    }
}
