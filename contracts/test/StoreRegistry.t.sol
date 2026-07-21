// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Test} from "forge-std/Test.sol";
import {StoreRegistry} from "../src/StoreRegistry.sol";

contract StoreRegistryTest is Test {
    StoreRegistry reg;
    address owner = address(0xA11CE);
    address merchant = address(0xB0B);
    address stranger = address(0xBAD);
    bytes32 constant META = keccak256("content-v1");

    event StoreRegistered(
        bytes32 indexed storeId, string slug, address indexed merchant, bytes32 metadataHash
    );
    event StoreMetadataUpdated(bytes32 indexed storeId, string slug, bytes32 metadataHash);

    function setUp() public {
        reg = new StoreRegistry(owner);
    }

    function testConstructorSetsOwner() public view {
        assertEq(reg.owner(), owner);
    }

    function testConstructorRejectsZeroOwner() public {
        vm.expectRevert(StoreRegistry.ZeroAddress.selector);
        new StoreRegistry(address(0));
    }

    function testStoreIdIsKeccakOfSlug() public view {
        assertEq(reg.storeId("acme"), keccak256(bytes("acme")));
    }

    function testRegisterStoresRecordAndEmits() public {
        bytes32 id = reg.storeId("acme");
        vm.expectEmit(true, true, true, true);
        emit StoreRegistered(id, "acme", merchant, META);
        vm.prank(owner);
        reg.register("acme", merchant, META);

        (address m, bytes32 meta, uint64 ts) = reg.stores(id);
        assertEq(m, merchant);
        assertEq(meta, META);
        assertGt(ts, 0);
    }

    function testRegisterRejectsDuplicate() public {
        vm.prank(owner);
        reg.register("acme", merchant, META);
        vm.prank(owner);
        vm.expectRevert(StoreRegistry.AlreadyRegistered.selector);
        reg.register("acme", merchant, META);
    }

    function testRegisterRejectsZeroMerchant() public {
        vm.prank(owner);
        vm.expectRevert(StoreRegistry.ZeroAddress.selector);
        reg.register("acme", address(0), META);
    }

    function testRegisterOnlyOwnerReverts() public {
        vm.prank(stranger);
        vm.expectRevert(StoreRegistry.NotOwner.selector);
        reg.register("acme", merchant, META);
    }

    function testUpdateMetadataEmitsAndStores() public {
        vm.prank(owner);
        reg.register("acme", merchant, META);

        bytes32 id = reg.storeId("acme");
        bytes32 meta2 = keccak256("content-v2");
        vm.expectEmit(true, true, true, true);
        emit StoreMetadataUpdated(id, "acme", meta2);
        vm.prank(owner);
        reg.updateMetadata("acme", meta2);

        (, bytes32 meta,) = reg.stores(id);
        assertEq(meta, meta2);
    }

    function testUpdateMetadataRejectsUnregistered() public {
        vm.prank(owner);
        vm.expectRevert(StoreRegistry.NotRegistered.selector);
        reg.updateMetadata("ghost", META);
    }

    function testUpdateMetadataOnlyOwnerReverts() public {
        vm.prank(owner);
        reg.register("acme", merchant, META);
        vm.prank(stranger);
        vm.expectRevert(StoreRegistry.NotOwner.selector);
        reg.updateMetadata("acme", keccak256("x"));
    }
}
